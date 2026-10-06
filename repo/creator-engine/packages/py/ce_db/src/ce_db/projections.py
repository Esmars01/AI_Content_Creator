"""Read-only projection tables rebuilt from the spec and the BuildManifest (I2, ADR 0002).

`scenes`, `scene_cast`, `shots` and the `selected` flag of `takes` are projections. They are
never edited directly: `rebuild_projections` deletes a version's projection rows and derives
them again from `video_versions.spec` (and, for timings and takes, the manifest artifacts once
they exist). Rebuilding twice gives identical rows.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import NotFoundError
from ce_core.spec.videospec import VideoSpec
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.videos import Scene, SceneCast, Shot, Take, VideoVersion
from ce_db.repository import OrgContext

__all__ = ["projection_rows", "rebuild_projections"]


def projection_rows(spec: VideoSpec) -> dict[str, list[dict[str, Any]]]:
    """The projection rows a spec implies (without ids, org or version)."""
    cast = {c.key: c for c in spec.cast}
    scenes: list[dict[str, Any]] = []
    scene_cast: list[dict[str, Any]] = []
    shots: list[dict[str, Any]] = []
    selected: dict[str, str | None] = {}
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        world = scene.world
        scenes.append(
            {
                "scene_key": scene.key,
                "order": scene.order,
                "purpose": scene.purpose,
                "world_version_id": world.world_version_id if world else None,
                "camera_position_key": world.camera_position_key if world else None,
                "time_of_day": world.time_of_day if world else None,
                "weather": world.weather if world else None,
            }
        )
        for member in scene.cast:
            spec_member = cast[member.character_key]
            scene_cast.append(
                {
                    "scene_key": scene.key,
                    "character_key": member.character_key,
                    "creator_version_id": spec_member.creator_version_id,
                    "appearance_version_id": spec_member.overrides.appearance_version_id,
                    "voice_version_id": spec_member.overrides.voice_version_id,
                    "wardrobe_version_id": member.wardrobe_version_id,
                }
            )
        for shot in scene.shots:
            shots.append(
                {
                    "scene_key": scene.key,
                    "shot_key": shot.key,
                    "type": shot.type,
                    "layer": shot.layer,
                    "selected_take_key": shot.takes.selected_take_key,
                }
            )
            selected[shot.key] = shot.takes.selected_take_key
    return {"scenes": scenes, "scene_cast": scene_cast, "shots": shots, "selected_takes": [selected]}


async def rebuild_projections(session: AsyncSession, ctx: OrgContext, version_id: UUID) -> None:
    version = (
        await session.execute(
            sa.select(VideoVersion).where(VideoVersion.org_id == ctx.org_id, VideoVersion.id == version_id)
        )
    ).scalar_one_or_none()
    if version is None:
        raise NotFoundError("video version not found")
    spec = VideoSpec.model_validate(version.spec)
    rows = projection_rows(spec)
    scope = {"org_id": ctx.org_id, "version_id": version_id}
    for model in (SceneCast, Shot, Scene):
        await session.execute(sa.delete(model).where(model.org_id == ctx.org_id, model.version_id == version_id))
    if rows["scenes"]:
        await session.execute(sa.insert(Scene), [scope | r for r in rows["scenes"]])
    if rows["scene_cast"]:
        await session.execute(sa.insert(SceneCast), [scope | r for r in rows["scene_cast"]])
    if rows["shots"]:
        await session.execute(sa.insert(Shot), [scope | r for r in rows["shots"]])
    selected = rows["selected_takes"][0]
    for shot_key, take_key in selected.items():
        await session.execute(
            sa.update(Take)
            .where(Take.org_id == ctx.org_id, Take.version_id == version_id, Take.shot_key == shot_key)
            .values(selected=Take.take_key == take_key if take_key else False)
        )
    await session.flush()
