"""Production data administration: `ce admin bootstrap` and `ce data purge-demo`.

- `ce admin bootstrap` creates the first organization and its owner on a clean install. Production
  never runs `ce seed dev`, so this is how a real deployment gets its first user.
- `ce data purge-demo` removes the dev seed's data from a database: every organization flagged
  `is_demo` (the seed's org, migration 0006), with all its rows; the users who belonged only to demo
  organizations; and the storage objects only those rows referenced. It is a dry run unless
  `--apply`. Rows of real organizations are never touched, and an object another row still uses is kept.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
import typer
from ce_config.settings import EffectiveConfig, load_effective
from ce_db.base import Base
from ce_db.models.assets import Artifact, Asset
from ce_db.models.platform import AuditLog
from ce_db.models.tenancy import Membership, OperatorProfile, Organization, User
from ce_db.session import Database
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.security.passwords import Passwords, password_issues

__all__ = ["BootstrapResult", "PurgePlan", "apply_purge", "bootstrap_org", "plan_purge"]

admin_app = typer.Typer(help="Organization administration.", no_args_is_help=True)
data_app = typer.Typer(help="Data hygiene (demo and seed data).", no_args_is_help=True)

ConfigRoot = Annotated[Path, typer.Option("--config-root", help="The config/ directory.")]
BOOTSTRAP_PASSWORD_ENV = "CE_BOOTSTRAP_PASSWORD"  # noqa: S105 - the variable name, not a password


# ---------------------------------------------------------------------- bootstrap
@dataclass(frozen=True)
class BootstrapResult:
    org_id: UUID
    user_id: UUID


async def bootstrap_org(
    session: AsyncSession,
    *,
    org_name: str,
    email: str,
    name: str,
    password_hash: str,
    platform_admin: bool,
    jurisdiction: str,
    revenue_band: str,
) -> BootstrapResult:
    """A real (non-demo) organization with its owner. Refuses an existing email or org name."""
    email = email.strip().lower()
    org_name = org_name.strip()
    if not org_name:
        raise ValueError("the organization name cannot be empty")
    if "@" not in email:
        raise ValueError("the owner's email is not an email address")
    if (await session.execute(sa.select(User.id).where(User.email == email))).first() is not None:
        raise ValueError(f"a user with email {email} already exists; invite them from the web app instead")
    if (await session.execute(sa.select(Organization.id).where(Organization.name == org_name))).first() is not None:
        raise ValueError(f"an organization named {org_name!r} already exists")
    org = Organization(name=org_name, plan="standard", is_demo=False, settings={})
    user = User(email=email, name=name.strip(), password_hash=password_hash, is_platform_admin=platform_admin)
    session.add_all([org, user])
    await session.flush()
    session.add_all(
        [
            Membership(user_id=user.id, org_id=org.id, role="owner"),
            OperatorProfile(
                org_id=org.id, jurisdiction=jurisdiction, regions_served=[jurisdiction], revenue_band=revenue_band
            ),
            AuditLog(
                org_id=org.id,
                actor_user_id=None,
                actor_kind="system",
                action="org.bootstrap",
                target_type="organization",
                target_id=str(org.id),
                after={"name": org_name, "owner": email, "platform_admin": platform_admin},
            ),
        ]
    )
    await session.flush()
    return BootstrapResult(org.id, user.id)


@admin_app.command("bootstrap")
def bootstrap_command(
    org_name: Annotated[str, typer.Option("--org-name", help="The organization's display name.")],
    email: Annotated[str, typer.Option("--email", help="The owner's sign-in email.")],
    name: Annotated[str, typer.Option("--name", help="The owner's display name.")] = "",
    platform_admin: Annotated[
        bool,
        typer.Option(
            "--platform-admin/--no-platform-admin",
            help="Also make the owner a platform administrator (GPU providers, model promotion).",
        ),
    ] = True,
    config_root: ConfigRoot = Path("config"),
) -> None:
    """Create the first organization and its owner on a clean (production) install.

    The password comes from CE_BOOTSTRAP_PASSWORD, or is asked for (hidden) on the terminal; it is
    never printed.
    """
    effective = load_effective(config_root)
    security = effective.bundle.app.security
    password = os.environ.get(BOOTSTRAP_PASSWORD_ENV) or typer.prompt(
        "Owner password", hide_input=True, confirmation_prompt=True
    )
    if issues := password_issues(password, security.password_min_length):
        typer.echo("password refused: " + "; ".join(i.message for i in issues), err=True)
        raise typer.Exit(2)
    password_hash = Passwords(security.password_hash).hash(password)

    async def run() -> BootstrapResult:
        database = Database(effective.settings.database_url, pool_size=1)
        try:
            async with database.transaction() as session:
                return await bootstrap_org(
                    session,
                    org_name=org_name,
                    email=email,
                    name=name,
                    password_hash=password_hash,
                    platform_admin=platform_admin,
                    jurisdiction=effective.settings.operator_jurisdiction,
                    revenue_band=effective.settings.operator_revenue_band,
                )
        finally:
            await database.dispose()

    try:
        result = asyncio.run(run())
    except ValueError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"organization {result.org_id} ({org_name})")
    typer.echo(
        f"owner        {result.user_id} ({email.strip().lower()}){' · platform admin' if platform_admin else ''}"
    )


# ---------------------------------------------------------------------- purge
@dataclass
class PurgePlan:
    orgs: list[tuple[UUID, str]] = field(default_factory=list)
    rows: dict[str, int] = field(default_factory=dict)  # table → rows in demo orgs
    users_delete: list[tuple[UUID, str]] = field(default_factory=list)  # members of demo orgs only
    objects: list[tuple[str, str]] = field(default_factory=list)  # (bucket, key) no real row uses
    objects_kept: int = 0  # referenced by a row of a real org as well

    @property
    def empty(self) -> bool:
        return not self.orgs

    def describe(self) -> list[str]:
        if self.empty:
            return ["no demo organization: nothing to purge"]
        lines = [f"demo organization {org_id} ({name})" for org_id, name in self.orgs]
        lines += [f"  {table:32} {count:>8} rows" for table, count in sorted(self.rows.items()) if count]
        lines += [
            f"user {email} ({user_id}): member of demo organizations only" for user_id, email in self.users_delete
        ]
        lines.append(f"storage objects only demo rows use: {len(self.objects)}")
        if self.objects_kept:
            lines.append(f"storage objects kept (a real organization uses them too): {self.objects_kept}")
        return lines


def _tenant_tables() -> list[sa.Table]:
    return [t for t in Base.metadata.sorted_tables if "org_id" in t.c and t.name != "organizations"]


async def plan_purge(session: AsyncSession, *, assets_bucket: str, artifacts_bucket: str) -> PurgePlan:
    plan = PurgePlan()
    plan.orgs = [
        (r[0], r[1])
        for r in (
            await session.execute(
                sa.select(Organization.id, Organization.name)
                .where(Organization.is_demo.is_(True))
                .order_by(Organization.id)
            )
        ).all()
    ]
    if plan.empty:
        return plan
    demo = [org_id for org_id, _ in plan.orgs]
    for table in _tenant_tables():
        count = (
            await session.execute(sa.select(sa.func.count()).select_from(table).where(table.c.org_id.in_(demo)))
        ).scalar_one()
        plan.rows[table.name] = int(count)
    real_member = sa.exists().where(Membership.user_id == User.id, Membership.org_id.not_in(demo))
    demo_member = sa.exists().where(Membership.user_id == User.id, Membership.org_id.in_(demo))
    plan.users_delete = [
        (r[0], r[1])
        for r in (
            await session.execute(sa.select(User.id, User.email).where(demo_member, ~real_member).order_by(User.email))
        ).all()
    ]
    for model, bucket in ((Asset, assets_bucket), (Artifact, artifacts_bucket)):
        keys = {
            r[0]
            for r in (await session.execute(sa.select(model.storage_key).where(model.org_id.in_(demo)))).all()
            if r[0]
        }
        for key in sorted(keys):
            shared = False
            for other in (Asset, Artifact):
                used = await session.execute(
                    sa.select(other.id).where(other.storage_key == key, other.org_id.not_in(demo)).limit(1)
                )
                shared = shared or used.first() is not None
            if shared:
                plan.objects_kept += 1
            else:
                plan.objects.append((bucket, key))
    return plan


async def apply_purge(session: AsyncSession, plan: PurgePlan) -> dict[str, Any]:
    """Deletes the planned rows in the caller's transaction. Storage objects are removed by the caller
    after the commit (an orphaned object is harmless; a row pointing at a deleted object is not)."""
    if plan.empty:
        return {"orgs": 0, "users_deleted": 0, "users_deactivated": 0}
    demo = [org_id for org_id, _ in plan.orgs]
    # The documented bypass for deletion workflows: append-only tables (audit logs) and approved
    # identity versions of the demo orgs go with them.
    await session.execute(sa.text("SET LOCAL ce.maintenance = 'on'"))
    await session.execute(sa.delete(Organization).where(Organization.id.in_(demo)))  # tenant rows cascade
    deleted = deactivated = 0
    for user_id, _ in plan.users_delete:
        try:
            async with session.begin_nested():
                await session.execute(sa.delete(User).where(User.id == user_id))
            deleted += 1
        except sa.exc.IntegrityError:
            # Still referenced by a real org's row (e.g. created_by): keep the identity, disable sign-in.
            await session.execute(sa.update(User).where(User.id == user_id).values(is_active=False, password_hash=None))
            deactivated += 1
    session.add(
        AuditLog(
            org_id=None,
            actor_user_id=None,
            actor_kind="system",
            action="data.purge_demo",
            target_type="organization",
            target_id=",".join(str(o) for o in demo),
            after={
                "orgs": [name for _, name in plan.orgs],
                "rows": {k: v for k, v in plan.rows.items() if v},
                "users_deleted": deleted,
                "users_deactivated": deactivated,
                "objects": len(plan.objects),
            },
        )
    )
    await session.flush()
    return {"orgs": len(demo), "users_deleted": deleted, "users_deactivated": deactivated}


async def _purge(effective: EffectiveConfig, *, apply: bool) -> tuple[PurgePlan, dict[str, Any] | None]:
    database = Database(effective.settings.database_url, pool_size=1)
    s = effective.settings
    try:
        async with database.transaction() as session:
            plan = await plan_purge(session, assets_bucket=s.s3_bucket_assets, artifacts_bucket=s.s3_bucket_artifacts)
            result = await apply_purge(session, plan) if apply and not plan.empty else None
    finally:
        await database.dispose()
    if result is not None and plan.objects:
        from ce_storage import create_storage

        storage = create_storage(s)
        removed = 0
        for bucket, key in plan.objects:
            await storage.delete(bucket, key)  # idempotent: an already missing object is fine
            removed += 1
        result["objects_deleted"] = removed
    return plan, result


@data_app.command("purge-demo")
def purge_demo_command(
    apply: Annotated[bool, typer.Option("--apply", help="Delete. Without it, only report what would go.")] = False,
    config_root: ConfigRoot = Path("config"),
) -> None:
    """Remove the dev seed's demo organization(s), their users and storage objects (dry run by default)."""
    effective = load_effective(config_root)
    plan, result = asyncio.run(_purge(effective, apply=apply))
    for line in plan.describe():
        typer.echo(line)
    if plan.empty:
        return
    if result is None:
        typer.echo("\ndry run: nothing was deleted. Re-run with --apply to delete.")
        return
    typer.echo(
        f"\ndeleted {result['orgs']} demo organization(s), {result['users_deleted']} user(s)"
        f" ({result['users_deactivated']} deactivated: still referenced),"
        f" {result.get('objects_deleted', 0)} storage object(s)"
    )
