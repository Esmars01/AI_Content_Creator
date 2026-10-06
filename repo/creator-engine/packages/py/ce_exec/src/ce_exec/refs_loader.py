"""Loads the approved records a spec references (`BuildRefs`) from the database, org-scoped."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_build.refs import (
    AppearanceRef,
    AssetInfo,
    BrandKitRef,
    BuildRefs,
    CreatorRef,
    SnapshotRef,
    VoiceRef,
    WardrobeRef,
    WorldRef,
)
from ce_core.errors import NotFoundError
from ce_core.spec.videospec import VideoSpec
from ce_core.spec.world import AssetContinuityRef, ShotContinuityRef
from ce_db.models.assets import Artifact, Asset
from ce_db.models.creators import AppearanceVersion, CreatorVersion, VoiceVersion, WardrobeVersion
from ce_db.models.memory import MemorySnapshot
from ce_db.models.research import BrandKit
from ce_db.models.videos import BuildManifestEntry
from ce_db.models.worlds import WorldVersion
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["load_assets", "load_refs", "load_shot_artifacts", "spec_asset_ids"]


async def load_assets(session: AsyncSession, org_id: UUID, ids: Iterable[UUID]) -> dict[UUID, AssetInfo]:
    wanted = sorted({i for i in ids if i is not None}, key=str)
    if not wanted:
        return {}
    rows = (await session.execute(sa.select(Asset).where(Asset.org_id == org_id, Asset.id.in_(wanted)))).scalars()
    found = {
        r.id: AssetInfo(
            asset_id=r.id, sha256=r.sha256, storage_key=r.storage_key, mime=r.mime, bytes=r.bytes, kind=r.kind
        )
        for r in rows
        if r.status == "ready"
    }
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise NotFoundError(f"assets not found or not ready: {missing}", table="assets")
    return found


def spec_asset_ids(spec: VideoSpec) -> set[UUID]:
    """Assets the spec reads directly (keyframes, B-roll, screen and reaction sources, music, SFX)."""
    ids: set[UUID] = set()
    for _, shot in spec.shots():
        if shot.visual is not None and shot.visual.keyframe.asset_id is not None:
            ids.add(shot.visual.keyframe.asset_id)
        if shot.broll is not None and shot.broll.asset_id is not None:
            ids.add(shot.broll.asset_id)
        if shot.screen is not None:
            ids.add(shot.screen.asset_id)
        if shot.reaction_source is not None:
            ids.add(shot.reaction_source.asset_id)
    ids.update(c.asset_id for c in spec.audio.music.cues if c.asset_id is not None)
    ids.update(s.asset_id for s in spec.audio.sfx if s.asset_id is not None)
    for scene in spec.scenes:
        ref = scene.world.continuity_ref if scene.world is not None else None
        if isinstance(ref, AssetContinuityRef):
            ids.add(ref.asset_id)
    return ids


async def load_shot_artifacts(session: AsyncSession, org_id: UUID, spec: VideoSpec) -> dict[str, str]:
    """`continuity_ref` of kind shot → the sha256 of the referenced shot's keyframe output
    (`image.keyframe:<shot>` in that version's BuildManifest), keyed `{version_id}:{shot_key}`."""
    out: dict[str, str] = {}
    for scene in spec.scenes:
        ref = scene.world.continuity_ref if scene.world is not None else None
        if not isinstance(ref, ShotContinuityRef):
            continue
        row = (
            await session.execute(
                sa.select(Artifact.sha256)
                .join(
                    BuildManifestEntry,
                    sa.and_(
                        BuildManifestEntry.org_id == Artifact.org_id, BuildManifestEntry.artifact_id == Artifact.id
                    ),
                )
                .where(
                    BuildManifestEntry.org_id == org_id,
                    BuildManifestEntry.version_id == ref.version_id,
                    BuildManifestEntry.node_key == f"image.keyframe:{ref.shot_key}",
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(
                f"continuity shot {ref.shot_key} of version {ref.version_id} has no keyframe",
                table="build_manifest_entries",
            )
        out[f"{ref.version_id}:{ref.shot_key}"] = str(row)
    return out


async def _one(session: AsyncSession, model: Any, org_id: UUID, row_id: UUID) -> Any:
    row = (
        await session.execute(sa.select(model).where(model.org_id == org_id, model.id == row_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"{model.__tablename__} {row_id} not found", table=model.__tablename__)
    return row


async def load_refs(
    session: AsyncSession,
    org_id: UUID,
    spec: VideoSpec,
    *,
    extra_snapshots: Mapping[UUID, SnapshotRef] | None = None,
) -> BuildRefs:
    """`extra_snapshots`: snapshots not persisted yet (a plan being finalized pins them by id)."""
    refs = BuildRefs()
    asset_ids: set[UUID] = spec_asset_ids(spec)
    appearance_ids: set[UUID] = set()
    voice_ids: set[UUID] = set()
    for member in spec.cast:
        row = await _one(session, CreatorVersion, org_id, member.creator_version_id)
        refs.creators[row.id] = CreatorRef(
            row.id, row.creator_id, row.dna, row.appearance_version_id, row.voice_version_id
        )
        appearance = member.overrides.appearance_version_id or row.appearance_version_id
        voice = member.overrides.voice_version_id or row.voice_version_id
        if appearance:
            appearance_ids.add(appearance)
        if voice:
            voice_ids.add(voice)
    appearance_rows = [await _one(session, AppearanceVersion, org_id, i) for i in sorted(appearance_ids, key=str)]
    voice_rows = [await _one(session, VoiceVersion, org_id, i) for i in sorted(voice_ids, key=str)]
    wardrobe_ids = {c.wardrobe_version_id for s in spec.scenes for c in s.cast if c.wardrobe_version_id}
    wardrobe_rows = [await _one(session, WardrobeVersion, org_id, i) for i in sorted(wardrobe_ids, key=str)]
    world_ids = {s.world.world_version_id for s in spec.scenes if s.world is not None}
    world_rows = [await _one(session, WorldVersion, org_id, i) for i in sorted(world_ids, key=str)]
    for row in appearance_rows:
        if row.canonical_face_asset_id:
            asset_ids.add(row.canonical_face_asset_id)
    for row in voice_rows:
        asset_ids.update(UUID(str(r["asset_id"])) for r in row.references or [] if r.get("asset_id"))
    for row in wardrobe_rows:
        asset_ids.update(row.reference_asset_ids or [])
    kit_row = (
        await _one(session, BrandKit, org_id, spec.brand.brand_kit_id) if spec.brand.brand_kit_id is not None else None
    )  # an archived kit still renders the versions that use it
    if kit_row is not None and kit_row.logo_asset_id is not None:
        asset_ids.add(kit_row.logo_asset_id)
    for row in world_rows:
        for times in (row.plates or {}).values():
            for weathers in times.values():
                asset_ids.update(UUID(str(a)) for a in weathers.values())
    refs.assets = await load_assets(session, org_id, asset_ids)
    for row in appearance_rows:
        face = refs.assets.get(row.canonical_face_asset_id) if row.canonical_face_asset_id else None
        refs.appearances[row.id] = AppearanceRef(row.id, row.dna, face)
    for row in voice_rows:
        refs.voices[row.id] = VoiceRef(
            version_id=row.id,
            description=row.description or "",
            references=tuple(row.references or []),
            reference_assets=tuple(
                refs.assets[UUID(str(r["asset_id"]))] for r in row.references or [] if r.get("asset_id")
            ),
            wpm={k: float(v) for k, v in (row.wpm or {}).items()},
            lexicon=tuple(row.lexicon or []),
            default_prosody=dict(row.default_prosody or {}),
        )
    for row in wardrobe_rows:
        refs.wardrobes[row.id] = WardrobeRef(
            row.id, row.spec, tuple(refs.assets[a] for a in row.reference_asset_ids or [] if a in refs.assets)
        )
    for row in world_rows:
        plates = {
            cam: {tod: {w: refs.assets[UUID(str(a))] for w, a in ws.items()} for tod, ws in tods.items()}
            for cam, tods in (row.plates or {}).items()
        }
        refs.worlds[row.id] = WorldRef(row.id, row.dna, plates)
    if kit_row is not None:
        refs.brand_kits[kit_row.id] = BrandKitRef(
            kit_id=kit_row.id,
            colors=dict(kit_row.colors or {}),
            fonts=dict(kit_row.fonts or {}),
            caption_style_id=kit_row.caption_style_id,
            logo=refs.assets.get(kit_row.logo_asset_id) if kit_row.logo_asset_id else None,
        )
    for pin in spec.memory.snapshots:
        if extra_snapshots and pin.snapshot_id in extra_snapshots:
            refs.snapshots[pin.snapshot_id] = extra_snapshots[pin.snapshot_id]
            continue
        snap = await _one(session, MemorySnapshot, org_id, pin.snapshot_id)
        refs.snapshots[snap.id] = SnapshotRef(snap.id, snap.digest, tuple(snap.items or []))
    refs.shot_artifacts = await load_shot_artifacts(session, org_id, spec)
    return refs
