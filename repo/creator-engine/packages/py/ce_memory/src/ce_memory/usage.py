"""The usage-log entry of a version (§18.3), one per cast member, written when the version reaches
`ready` (and `exported` in Phase 12). The repetition guard and the consistency reports read it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from ce_core.spec.videospec import VideoSpec

from ce_memory.text import ngram_fingerprints

__all__ = ["usage_payload"]


def usage_payload(
    spec: VideoSpec,
    character_key: str,
    *,
    behavior_signatures: Sequence[Mapping[str, Any]] = (),
    ngram: int = 4,
) -> dict[str, Any]:
    brief = spec.brief
    hooks: list[str] = []
    if brief is not None and brief.selected_hook_key:
        hooks = [h.text for h in brief.hook_candidates if h.key == brief.selected_hook_key]
    lines = " ".join(s.text for s in spec.script.segments if s.speaker_key == character_key)
    labels: list[str] = []
    intensities: list[float] = []
    shots: list[str] = []
    camera_position = time_of_day = None
    worlds: list[UUID] = []
    wardrobes: list[UUID] = []
    wardrobe = None
    for scene in sorted(spec.scenes, key=lambda s: s.order):
        if scene.acting is not None:
            for state in scene.acting.states:
                if state.character_key == character_key and state.emotion is not None:
                    labels.append(str(state.emotion.displayed.label))
                    intensities.append(float(state.emotion.displayed.intensity))
        shots += [f"{shot.type}:{shot.camera.framing}" for shot in scene.shots if str(shot.layer) == "base"]
        if scene.world is not None:
            worlds.append(scene.world.world_version_id)
            camera_position = camera_position or scene.world.camera_position_key
            time_of_day = time_of_day or str(scene.world.time_of_day)
        for member in scene.cast:
            if member.character_key == character_key and member.wardrobe_version_id:
                wardrobes.append(member.wardrobe_version_id)
                wardrobe = wardrobe or str(member.wardrobe_version_id)
    return {
        "hooks": hooks,
        "phrase_fingerprints": ngram_fingerprints(lines, ngram),
        "arc_signature": {"labels": labels, "intensities": intensities},
        "behavior_signatures": [dict(s) for s in behavior_signatures],
        "visual_signature": {
            "shot_sequence": shots,
            "camera_position": camera_position,
            "time_of_day": time_of_day,
            "wardrobe": wardrobe,
        },
        "world_version_ids": list(dict.fromkeys(worlds)),
        "wardrobe_version_ids": list(dict.fromkeys(wardrobes)),
    }
