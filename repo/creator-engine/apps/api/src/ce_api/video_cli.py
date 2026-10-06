"""`ce video generate-fixture`: the §11 example spec (or its two-scene split, `--fixture
two_scene`, the Phase 6 edit fixture) as an approved version, built by `GenerateVersionWorkflow`
(`make e2e-mock`, the Playwright edit test). Planning and approval replace this path for real
videos. Refuses to run with `APP_ENV=prod`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer
from ce_config.settings import load_effective

video_app = typer.Typer(help="Videos (development tools).", no_args_is_help=True)

ConfigRoot = Annotated[Path, typer.Option("--config-root", help="The config/ directory.")]


async def generate_fixture(
    config_root: Path,
    *,
    wait: bool,
    out: Path | None,
    seed_namespace: str | None,
    timeout_s: float,
    fixture: str = "example",
    project_id: UUID | None = None,
) -> dict[str, Any]:
    import sqlalchemy as sa
    from ce_db.models.assets import Artifact
    from ce_db.models.videos import Render
    from ce_exec.context import build_services
    from ce_exec.submit import submit_spec
    from ce_testing.fixtures import ALEX, example_spec_dict, two_scene_spec_dict
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    effective = load_effective(config_root)
    if effective.settings.app_env == "prod":
        raise typer.BadParameter("refusing to submit fixture videos with APP_ENV=prod")
    svc = build_services(effective, events=True, pool_size=2)
    try:
        fixtures = {"example": example_spec_dict, "two_scene": two_scene_spec_dict}
        if fixture not in fixtures:
            raise typer.BadParameter(f"unknown fixture {fixture!r} (one of {', '.join(fixtures)})")
        spec = fixtures[fixture]()
        if seed_namespace:
            spec["generation"]["seed_namespace"] = seed_namespace
        submitted = await submit_spec(
            svc, org_id=ALEX.ORG_ID, project_id=project_id or ALEX.PROJECT_ID, spec_data=spec, user_id=ALEX.USER_ID
        )
        settings = effective.settings
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace, data_converter=pydantic_data_converter
        )
        prefix = settings.temporal_task_queue_prefix
        cfg = effective.bundle.app.build
        arg = {
            "org_id": str(ALEX.ORG_ID),
            "job_id": str(submitted.job_id),
            "version_id": str(submitted.version_id),
            "queues": {"orchestrator": f"{prefix}orchestrator", "render": f"{prefix}render"},
            "max_parallel": cfg.max_parallel_nodes,
            "model_timeout_s": cfg.model_node_timeout_s,
            "cpu_timeout_s": cfg.cpu_node_timeout_s,
            "render_timeout_s": cfg.render_node_timeout_s,
        }
        started = time.monotonic()
        handle = await client.start_workflow(
            "GenerateVersionWorkflow", arg, id=submitted.workflow_id, task_queue=f"{prefix}orchestrator"
        )
        summary: dict[str, Any] = {
            "video_id": str(submitted.video_id),
            "version_id": str(submitted.version_id),
            "job_id": str(submitted.job_id),
            "workflow_id": submitted.workflow_id,
        }
        if not wait:
            return summary
        result = await asyncio.wait_for(handle.result(), timeout_s)
        statuses: dict[str, str] = result.get("statuses", {})
        summary.update(
            {
                "state": result.get("state"),
                "seconds": round(time.monotonic() - started, 1),
                "nodes": len(statuses),
                "cached": sum(1 for s in statuses.values() if s == "cached"),
                "failed": result.get("failed", []),
            }
        )
        async with svc.db.session() as session:
            row = (
                await session.execute(
                    sa.select(Render, Artifact)
                    .join(Artifact, sa.and_(Artifact.org_id == Render.org_id, Artifact.id == Render.artifact_id))
                    .where(Render.version_id == UUID(summary["version_id"]), Render.is_proxy.is_(False))
                )
            ).first()
        if row is not None:
            render, artifact = row
            summary["render_id"] = str(render.id)
            summary["render_sha256"] = artifact.sha256
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                await svc.storage.download(settings.s3_bucket_artifacts, artifact.storage_key, out)
                summary["file"] = str(out)
        return summary
    finally:
        await svc.close()


@video_app.command("generate-fixture")
def generate_fixture_command(
    config_root: ConfigRoot = Path("config"),
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for the build to end.")] = True,
    out: Annotated[Path | None, typer.Option("--out", help="Download the final render here.")] = None,
    seed_namespace: Annotated[
        str | None, typer.Option("--seed-namespace", help="A UUID; a new one gives a fresh cache.")
    ] = None,
    timeout_s: Annotated[float, typer.Option("--timeout", help="Seconds to wait.")] = 900.0,
    fixture: Annotated[str, typer.Option("--fixture", help="example | two_scene")] = "example",
) -> None:
    """Submit the §11 example spec (creator Alex, from `ce seed dev`) and build it."""
    summary = asyncio.run(
        generate_fixture(
            config_root, wait=wait, out=out, seed_namespace=seed_namespace, timeout_s=timeout_s, fixture=fixture
        )
    )
    typer.echo(json.dumps(summary, indent=2))
    if wait and summary.get("state") != "ready":
        raise typer.Exit(1)
