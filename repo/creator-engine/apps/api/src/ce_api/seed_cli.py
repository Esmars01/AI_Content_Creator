"""`ce seed dev` (§40 Phase 1) and `ce storage init`.

`ce seed dev` applies the idempotent dev seed (`ce_testing.seed`), uploads its placeholder media
to the assets bucket, and gives the dev admin a password: `CE_SEED_ADMIN_PASSWORD` when set,
otherwise a random one printed once. An existing password is kept unless `--reset-password`.
It refuses to run with `APP_ENV=prod`.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
import typer
from ce_config.settings import EffectiveConfig, load_effective
from ce_db.session import Database

from ce_api.security.passwords import Passwords, password_issues

seed_app = typer.Typer(help="Seed development data.", no_args_is_help=True)
storage_app = typer.Typer(help="Object storage administration.", no_args_is_help=True)

ConfigRoot = Annotated[Path, typer.Option("--config-root", help="The config/ directory.")]
PASSWORD_ENV = "CE_SEED_ADMIN_PASSWORD"  # noqa: S105 - the variable name, not a password; CLI input only


async def ensure_buckets(effective: EffectiveConfig) -> list[str]:
    from ce_storage import create_storage

    storage = create_storage(effective.settings)
    buckets = [effective.settings.s3_bucket_assets, effective.settings.s3_bucket_artifacts]
    for bucket in buckets:
        await storage.ensure_bucket(bucket)
    return buckets


async def run_seed(
    effective: EffectiveConfig, *, reset_password: bool, skip_storage: bool
) -> tuple[dict[str, str], str | None]:
    """Returns the seeded ids and the generated password (None when none was generated)."""
    from ce_db.models.tenancy import User
    from ce_testing.seed import DEV_ADMIN_EMAIL, placeholder_objects, seed_dev

    security = effective.bundle.app.security
    chosen = os.environ.get(PASSWORD_ENV) or None
    if chosen is not None and (issues := password_issues(chosen, security.password_min_length)):
        raise typer.BadParameter(f"{PASSWORD_ENV}: " + "; ".join(i.message for i in issues))
    passwords = Passwords(security.password_hash)
    generated: str | None = None
    # Storage first: if it fails, nothing is committed and no generated password is lost.
    if not skip_storage:
        from ce_storage import create_storage

        storage = create_storage(effective.settings)
        bucket = effective.settings.s3_bucket_assets
        await storage.ensure_bucket(bucket)
        for item in placeholder_objects():
            await storage.put(bucket, item.key, item.data, content_type=item.mime, metadata={"placeholder": "true"})
    database = Database(effective.settings.database_url, pool_size=1)
    try:
        async with database.transaction() as session:
            ids = await seed_dev(session, effective.bundle.vocab)
            current = (
                await session.execute(sa.select(User.password_hash).where(User.email == DEV_ADMIN_EMAIL))
            ).scalar_one()
            if current is None or reset_password:
                if chosen is None:
                    generated = secrets.token_urlsafe(18)
                await session.execute(
                    sa.update(User)
                    .where(User.email == DEV_ADMIN_EMAIL)
                    .values(password_hash=passwords.hash(chosen or generated or ""))
                )
    finally:
        await database.dispose()
    return {k: str(v) for k, v in ids.items()}, generated


def _effective(config_root: Path) -> EffectiveConfig:
    effective = load_effective(config_root)
    if effective.settings.app_env == "prod":
        typer.echo("refusing to seed development data with APP_ENV=prod", err=True)
        raise typer.Exit(2)
    return effective


@seed_app.command("dev")
def seed_dev_command(
    config_root: ConfigRoot = Path("config"),
    reset_password: Annotated[
        bool, typer.Option("--reset-password", help="Replace an existing admin password.")
    ] = False,
    skip_storage: Annotated[bool, typer.Option("--skip-storage", help="Do not upload the placeholder media.")] = False,
) -> None:
    """Create the dev org, admin, creator Alex, wardrobe, world and memory (idempotent)."""
    from ce_testing.seed import DEV_ADMIN_EMAIL

    effective = _effective(config_root)
    ids, generated = asyncio.run(run_seed(effective, reset_password=reset_password, skip_storage=skip_storage))
    for name, value in ids.items():
        typer.echo(f"{name:18} {value}")
    if generated is not None:
        typer.echo(f"\nadmin login: {DEV_ADMIN_EMAIL}\npassword (shown once): {generated}")
        typer.echo(f"Set {PASSWORD_ENV} to choose the password instead.")
    elif os.environ.get(PASSWORD_ENV) and reset_password:
        typer.echo(f"\nadmin login: {DEV_ADMIN_EMAIL} (password from {PASSWORD_ENV})")
    else:
        typer.echo(f"\nadmin login: {DEV_ADMIN_EMAIL} (password unchanged; --reset-password to replace it)")


@storage_app.command("init")
def storage_init_command(
    config_root: ConfigRoot = Path("config"),
) -> None:
    """Create the assets and artifacts buckets if they do not exist."""
    effective = load_effective(config_root)
    for bucket in asyncio.run(ensure_buckets(effective)):
        typer.echo(f"bucket ready: {bucket}")
