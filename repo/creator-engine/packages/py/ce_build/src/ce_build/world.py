"""What a scene's world binding contributes to each node (§19.5 exact invalidation).

- **Plate view** (`world.plate`, `qc.world`): the camera position, time of day and weather, the
  lighting override, and only the overrides a plate from that position can show — states, hiding
  and moves of elements visible from it (its background layout plus the elements continuity says
  must show; every element when the position has no layout) and of practical lights (they light
  the room from anywhere), and every added element (its visibility cannot be known). Acoustics
  never reach the plate.
- **Behavior view** (`behavior.resolve`): overrides of elements the CBS can depend on — elements
  offered as attention targets (`behavior.attention_element_kinds`), furniture (affordances), and
  elements the acting plan targets (state attention targets, event targets, the stimulus).

The request builders use the same views, so equal digests always mean equal requests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from ce_core.spec.videospec import Scene
from ce_core.spec.world import WorldBinding

from ce_build.refs import WorldRef

__all__ = ["acting_targets", "behavior_view", "plate_relevant", "plate_view", "plate_view_is_empty"]


def plate_relevant(world: WorldRef, camera_position: str) -> set[str] | None:
    """Element keys whose state, visibility or position a plate from `camera_position` shows
    (None: no layout is known, so every element counts)."""
    visible = world.visible_elements(camera_position)
    if visible is None:
        return None
    practicals = {p.get("element") for p in (world.dna.get("lighting") or {}).get("practicals", [])}
    return visible | {p for p in practicals if isinstance(p, str)}


def plate_view(binding: WorldBinding, world: WorldRef) -> dict[str, Any]:
    """The binding as a plate from its camera position sees it (content only, no record ids)."""
    overrides = binding.overrides
    relevant = plate_relevant(world, binding.camera_position_key)
    defaults = {e.get("key"): e.get("default_state") for e in world.dna.get("elements", [])}

    def shows(key: str) -> bool:
        return relevant is None or key in relevant

    return {
        "camera_position_key": binding.camera_position_key,
        "time_of_day": str(binding.time_of_day),
        "weather": str(binding.weather),
        "element_states": {
            k: v for k, v in sorted(overrides.element_states.items()) if shows(k) and defaults.get(k) != v
        },
        "hidden": sorted(k for k in overrides.hide_elements if shows(k)),
        "added": [a.model_dump(mode="json") for a in overrides.add_elements],
        "moved": sorted(
            ([m.key, list(m.position)] for m in overrides.move_elements if shows(m.key)), key=lambda x: str(x[0])
        ),
        "lighting": overrides.lighting.model_dump(mode="json") if overrides.lighting else None,
    }


def plate_view_is_empty(view: Mapping[str, Any]) -> bool:
    return not (view["element_states"] or view["hidden"] or view["added"] or view["moved"] or view["lighting"])


def acting_targets(scene: Scene) -> set[str]:
    """World elements the scene's acting plan points at."""
    out: set[str] = set()
    acting = scene.acting
    if acting is None:
        return out
    for state in acting.states:
        if isinstance(state.attention_target, str) and state.attention_target.startswith("el_"):
            out.add(state.attention_target)
    for event in acting.events:
        if isinstance(event.target, str) and event.target.startswith("el_"):
            out.add(event.target)
    stimulus = acting.situation.stimulus
    if stimulus is not None and str(stimulus.kind) == "element":
        out.add(stimulus.ref)
    return out


def behavior_view(scene: Scene, world: WorldRef | None, attention_kinds: Iterable[str]) -> dict[str, Any] | None:
    """The overrides the CBS can depend on (see the module docstring)."""
    if scene.world is None:
        return None
    overrides = scene.world.overrides
    kinds = {e.get("key"): str(e.get("kind")) for e in (world.dna.get("elements", []) if world else [])}
    targets = acting_targets(scene)
    offered = set(attention_kinds) | {"furniture"}

    def relevant(key: str, kind: str | None = None) -> bool:
        return key in targets or (kind or kinds.get(key, "")) in offered

    return {
        "hidden": sorted(k for k in overrides.hide_elements if relevant(k)),
        "added": [
            {"key": a.key, "kind": str(a.kind), "label": a.label}
            for a in overrides.add_elements
            if relevant(a.key, str(a.kind))
        ],
        "states": {k: v for k, v in sorted(overrides.element_states.items()) if k in targets},
        "moved": {m.key: list(m.position) for m in overrides.move_elements if m.key in targets},
    }
