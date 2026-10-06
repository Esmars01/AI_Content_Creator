"""Parent builds for derived versions (§12.4): the parent's replayed graph, its manifest, its spec
and the sha256 of every node output it recorded (a voice lock reuses compiled prosody by content),
plus the behavior evaluator that keeps unchanged behavior outputs clean (§12.9)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_behavior.evaluate import BehaviorEvaluator, VersionInputs
from ce_build import BuildOptions, ParentBuild, assemble, parent_build
from ce_build.manifest import NodeRecord
from ce_db import execution as rec
from ce_db.models.assets import Artifact

from ce_exec.context import ExecServices, VersionData

__all__ = ["artifact_shas", "evaluator_for", "load_parent"]


async def artifact_shas(session: Any, org_id: UUID, artifact_map: dict[str, str]) -> dict[str, str]:
    """node key → sha256 of the artifact the manifest records for it."""
    ids = sorted({UUID(a) for a in artifact_map.values()}, key=str)
    if not ids:
        return {}
    rows = (
        await session.execute(
            sa.select(Artifact.id, Artifact.sha256).where(Artifact.org_id == org_id, Artifact.id.in_(ids))
        )
    ).all()
    by_id = {str(i): sha for i, sha in rows}
    return {key: by_id[a] for key, a in artifact_map.items() if a in by_id}


async def load_parent(
    svc: ExecServices, org_id: UUID, parent_version_id: UUID, options: BuildOptions
) -> tuple[ParentBuild, VersionData] | None:
    """The parent's build, or None when it recorded no manifest (it never generated)."""
    async with svc.db.session() as session:
        rows = await rec.load_manifest_rows(session, org_id, parent_version_id)
        if not rows:
            return None
        manifest = assemble([NodeRecord.from_row(r) for r in rows])
        outputs = await artifact_shas(session, org_id, dict(manifest.artifact_map))
    parent = await svc.version(org_id, parent_version_id)
    pb = parent_build(parent.spec, parent.refs, svc.bundle, parent.catalog or svc.catalog, manifest, options=options)
    return replace(pb, spec=parent.spec, outputs=outputs), parent


def evaluator_for(svc: ExecServices, parent: VersionData | None, child: VersionData | Any) -> BehaviorEvaluator:
    """`child` is a VersionData or anything with `spec` and `refs` (a proposed spec)."""
    old = VersionInputs(parent.spec, parent.refs) if parent is not None else None
    catalog = getattr(child, "catalog", None) or svc.catalog
    return BehaviorEvaluator(old, VersionInputs(child.spec, child.refs), svc.bundle, catalog)
