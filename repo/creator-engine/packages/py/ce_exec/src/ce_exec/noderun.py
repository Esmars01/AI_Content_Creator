"""One node's execution context: the version, the graph node, its upstream outputs and helpers
shared by the request builders (model nodes) and the in-process executors (CPU and render nodes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from ce_build.graph import shot_words
from ce_build.refs import AssetInfo
from ce_contracts.common import ArtifactRef
from ce_core.build import ExecutionGraph, ExecutionNode
from ce_core.spec.anchors import WordSpan
from ce_core.spec.videospec import Scene, Shot, VideoSpec
from ce_render.timeline import SegmentAudio, ShotAudio, Timeline, build_timeline, local_shot_audio
from ce_router import RouterCatalog
from ce_storage.content import StorageRunContext

from ce_exec.context import ExecServices, VersionData
from ce_exec.outputs import NodeOutput

__all__ = ["NodeRun", "artifact_refs_in"]


def artifact_refs_in(value: Any) -> list[ArtifactRef]:
    """Every `ArtifactRef` inside a request or result model (the inputs a worker must be granted)."""
    found: list[ArtifactRef] = []

    def walk(v: Any) -> None:
        if isinstance(v, ArtifactRef):
            found.append(v)
        elif hasattr(v, "__pydantic_fields__"):
            for name in type(v).model_fields:
                walk(getattr(v, name))
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, (list, tuple)):
            for item in v:
                walk(item)

    walk(value)
    unique: dict[str, ArtifactRef] = {}
    for ref in found:
        unique.setdefault(ref.sha256, ref)
    return list(unique.values())


@dataclass
class NodeRun:
    svc: ExecServices
    data: VersionData
    graph: ExecutionGraph
    node: ExecutionNode
    upstream: dict[str, NodeOutput]
    upstream_shas: dict[str, str]
    job_id: UUID
    ctx: StorageRunContext
    extra: dict[str, Any] = field(default_factory=dict)
    qc_retry: int = 0  # the QC attempt this run belongs to (0: the first; labels QC requests)

    # ------------------------------------------------------------------ spec lookups
    @property
    def catalog(self) -> RouterCatalog:
        """The router catalog pinned to the version (its registry snapshot, I5)."""
        return self.data.catalog or self.svc.catalog

    @property
    def spec(self) -> VideoSpec:
        return self.data.spec

    @property
    def org_id(self) -> UUID:
        return self.data.org_id

    @property
    def scene(self) -> Scene:
        if self.node.scene_key is None:
            raise ValueError(f"{self.node.key} has no scene")
        return self.spec.scene(self.node.scene_key)

    @property
    def shot(self) -> Shot:
        if self.node.shot_key is None:
            raise ValueError(f"{self.node.key} has no shot")
        return next(s for _, s in self.spec.shots() if s.key == self.node.shot_key)

    @property
    def scratch(self) -> Path:
        return self.ctx.scratch_dir

    # ------------------------------------------------------------------ upstream
    def deps(self, prefix: str) -> list[tuple[str, NodeOutput]]:
        return [(k, self.upstream[k]) for k in self.node.deps if k.startswith(prefix)]

    def dep(self, prefix: str) -> NodeOutput:
        found = self.deps(prefix)
        if not found:
            raise LookupError(f"{self.node.key} has no dependency {prefix}*")
        return found[0][1]

    def maybe(self, prefix: str) -> NodeOutput | None:
        found = self.deps(prefix)
        return found[0][1] if found else None

    def segment_audio(self, outputs: list[tuple[str, NodeOutput]] | None = None) -> dict[str, SegmentAudio]:
        """Aligned segments from `align.segment` outputs (all deps by default)."""
        out: dict[str, SegmentAudio] = {}
        for key, output in outputs if outputs is not None else self.deps("align.segment:"):
            segment = key.split(":", 1)[1]
            out[segment] = SegmentAudio(
                duration_s=float(output.data["duration_s"]),
                words=tuple((float(a), float(b)) for a, b in output.data["words"]),
            )
        return out

    def tts_audio(self, segment_key: str) -> ArtifactRef:
        align = self.upstream.get(f"align.segment:{segment_key}")
        if align is not None:
            return align.ref("audio")
        return self.upstream[f"tts.segment:{segment_key}"].ref("audio")

    def timeline(self) -> Timeline:
        cfg = self.svc.bundle.app.render.timeline
        return build_timeline(
            self.spec, self.segment_audio(), lead_s=cfg.lead_s, gap_s=cfg.segment_gap_s, tail_s=cfg.tail_s
        )

    def shot_audio(self, shot_key: str | None = None, *, pad: bool = True) -> ShotAudio:
        cfg = self.svc.bundle.app.render.timeline
        return local_shot_audio(
            self.spec,
            shot_key or self.shot.key,
            self.segment_audio(),
            gap_s=cfg.segment_gap_s,
            pad_in_s=cfg.shot_pad_in_s if pad else 0.0,
            pad_out_s=cfg.shot_pad_out_s if pad else 0.0,
        )

    def shot_word_list(self, shot: Shot | None = None) -> list[tuple[str, int]]:
        shot = shot or self.shot
        if not isinstance(shot.span, WordSpan):
            return []
        scene = next(sc for sc, s in self.spec.shots() if s.key == shot.key)
        return shot_words(self.spec, scene, shot.span)

    # ------------------------------------------------------------------ artifacts
    async def adopt(self, asset: AssetInfo, *, kind: str, role: str = "") -> ArtifactRef:
        """An uploaded asset as a content-addressed artifact (copied into the artifacts bucket once)."""
        await self.svc.content.adopt(
            self.svc.settings.s3_bucket_assets, asset.storage_key, asset.sha256, mime=asset.mime
        )
        return ArtifactRef(sha256=asset.sha256, kind=kind, mime=asset.mime, bytes=asset.bytes, role=role)

    def asset(self, name: str = "source") -> AssetInfo:
        asset_id = self.node.asset_inputs.get(name)
        if asset_id is None:
            raise LookupError(f"{self.node.key} has no asset input {name!r}")
        return self.data.refs.asset(UUID(asset_id))

    async def write(self, path: Path, kind: str, *, role: str = "", mime: str = "", **meta: Any) -> ArtifactRef:
        return await self.ctx.write_artifact(path, kind, dict(meta), role=role, mime=mime)

    async def fetch(self, ref: ArtifactRef) -> Path:
        return await self.ctx.read_artifact(ref)
