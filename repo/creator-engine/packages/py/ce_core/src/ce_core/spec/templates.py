"""Spec templates (§30 "Templates and brand", `spec_templates`, Phase 12, ADR 0059).

A spec template is a **partial spec** a user saved from a version, made only of portable
**slots**: values that mean the same in any video (caption style, brand kit, platform targets,
render outputs, the talking-shot camera, scene pacing). Nothing keyed to one video's words, shots
or scenes is ever captured, so applying a template can never point at an element that does not
exist.

- **Kinds** (`video, scene, creator_style, camera, caption, brand`) choose which slots a template
  may hold (`KIND_SLOTS`); `paths` narrows that further (a slot path or a section prefix).
- **Body** layout: the spec's own sections for spec-level values (`meta`, `captions`, `brand`,
  `render`, `provenance`), plus `shot_defaults.camera` (applied to every talking shot) and
  `scene_defaults.pacing` (applied to every scene) for values that live on many elements.
- **Compose**: deep-merge the bodies in `composes_from` order, then the template itself. Later wins
  for scalars and objects; lists are replaced unless the slot declares `merge: append` (platform
  targets, render outputs — de-duplicated). A later value that differs from an earlier one is a
  **conflict**, reported in the preview with the value that won.
- **Apply** turns a body into ordinary edit operations (`set_meta`, `set_captions`, `set_brand`,
  `set_render_outputs`, `set_provenance_label`, `set_camera`, `set_pacing`), so a template goes
  through the same edit proposal, validation, locks and diff as any edit (I3); slots whose value
  the version already has produce no operation.

Pure functions, no I/O; no model, provider or platform names (rule 7).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, PositiveFloat, PositiveInt, ValidationError

from ce_core.enums import QualityTier
from ce_core.scalars import Aspect, LanguageTag, Token
from ce_core.spec.base import SpecModel
from ce_core.spec.videospec import OutputPreset, Reframe

__all__ = [
    "KIND_SLOTS",
    "SLOTS",
    "Composed",
    "Conflict",
    "Slot",
    "TemplateBody",
    "TemplateError",
    "body_digest",
    "capture",
    "compose",
    "operations",
    "select_slots",
    "slot_values",
    "validate_body",
]

TemplateKind = Literal["video", "scene", "creator_style", "camera", "caption", "brand"]


class TemplateError(ValueError):
    """An invalid template request (unknown kind, path or slot value)."""

    def __init__(self, message: str, problems: Sequence[tuple[str, str]] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)  # (path, message)


# ---------------------------------------------------------------------- the body (strict)


class _Meta(SpecModel):
    platform_targets: list[Token] | None = None
    primary_aspect: Aspect | None = None
    target_duration_s: PositiveFloat | None = None
    quality_tier: QualityTier | None = None


class _Captions(SpecModel):
    enabled: bool | None = None
    style_id: Token | None = None
    language: LanguageTag | None = None
    max_words_per_line: PositiveInt | None = None
    highlight: Literal["active_word", "phrase", "none"] | None = None
    placement: Literal["platform_safe_zone", "bottom", "center", "top"] | None = None


class _Brand(SpecModel):
    brand_kit_id: UUID | None = None
    logo_overlay: bool | None = None


class _Render(SpecModel):
    outputs: list[OutputPreset] | None = None
    reframe: Reframe | None = None


class _Provenance(SpecModel):
    visible_label: Literal["auto", "on", "off"] | None = None


class _Camera(SpecModel):
    profile_id: Token | None = None
    framing: Token | None = None
    angle: Token | None = None


class _ShotDefaults(SpecModel):
    camera: _Camera | None = None


class _Pacing(SpecModel):
    cut_cadence: Literal["slow", "medium", "fast"] | None = None
    target_wpm_delta: float | None = Field(default=None, ge=-0.5, le=0.5)


class _SceneDefaults(SpecModel):
    pacing: _Pacing | None = None


class TemplateBody(SpecModel):
    """The only shape a template body may have: spec sections and the shot/scene defaults, every
    value typed as in the VideoSpec. Unknown keys are refused."""

    meta: _Meta | None = None
    captions: _Captions | None = None
    brand: _Brand | None = None
    render: _Render | None = None
    provenance: _Provenance | None = None
    shot_defaults: _ShotDefaults | None = None
    scene_defaults: _SceneDefaults | None = None


def validate_body(body: Mapping[str, Any], kind: str | None = None) -> dict[str, Any]:
    """The body in canonical JSON form, or TemplateError with each problem's path. With `kind`,
    slots the kind does not hold are refused too."""
    try:
        parsed = TemplateBody.model_validate(body)
    except ValidationError as exc:
        problems = [("/" + "/".join(str(p) for p in e["loc"]), e["msg"]) for e in exc.errors()[:20]]
        raise TemplateError("the template body is invalid", problems) from exc
    clean = parsed.model_dump(mode="json", exclude_none=True)
    if kind is not None:
        allowed = set(select_slots(kind))
        outside = sorted(p for p in slot_values(clean) if p not in allowed)
        if outside:
            raise TemplateError(
                f"a {kind} template cannot hold {', '.join(outside)}", [(p, "not a slot of this kind") for p in outside]
            )
    return clean


@dataclass(frozen=True)
class Slot:
    path: str  # "/captions/style_id": where the value lives in a template body
    merge: Literal["replace", "append"] = "replace"
    list_key: str | None = None  # append-merge de-duplication key of list items (None: the item itself)


SLOTS: dict[str, Slot] = {
    s.path: s
    for s in (
        Slot("/meta/platform_targets", merge="append"),
        Slot("/meta/primary_aspect"),
        Slot("/meta/target_duration_s"),
        Slot("/meta/quality_tier"),
        Slot("/captions/enabled"),
        Slot("/captions/style_id"),
        Slot("/captions/language"),
        Slot("/captions/max_words_per_line"),
        Slot("/captions/highlight"),
        Slot("/captions/placement"),
        Slot("/brand/brand_kit_id"),
        Slot("/brand/logo_overlay"),
        Slot("/render/outputs", merge="append", list_key="preset_id"),
        Slot("/render/reframe"),
        Slot("/provenance/visible_label"),
        Slot("/shot_defaults/camera/profile_id"),
        Slot("/shot_defaults/camera/framing"),
        Slot("/shot_defaults/camera/angle"),
        Slot("/scene_defaults/pacing/cut_cadence"),
        Slot("/scene_defaults/pacing/target_wpm_delta"),
    )
}

_CAMERA = ("/shot_defaults/camera/profile_id", "/shot_defaults/camera/framing", "/shot_defaults/camera/angle")
_PACING = ("/scene_defaults/pacing/cut_cadence", "/scene_defaults/pacing/target_wpm_delta")
_CAPTION_STYLE = (
    "/captions/style_id",
    "/captions/max_words_per_line",
    "/captions/highlight",
    "/captions/placement",
)

KIND_SLOTS: dict[str, tuple[str, ...]] = {
    "video": tuple(SLOTS),
    "scene": (*_CAMERA, *_PACING),
    "creator_style": (*_CAMERA, *_PACING, *_CAPTION_STYLE),
    "camera": _CAMERA,
    "caption": tuple(p for p in SLOTS if p.startswith("/captions/")),
    "brand": ("/brand/brand_kit_id", "/brand/logo_overlay", *_CAPTION_STYLE),
}


def select_slots(kind: str, paths: Sequence[str] | None = None) -> list[str]:
    """The slots a template of `kind` holds; `paths` (slot paths or section prefixes such as
    `/captions`) narrows them. Unknown kinds, and paths that name no slot of the kind, are refused."""
    if kind not in KIND_SLOTS:
        raise TemplateError(f"unknown template kind {kind!r}")
    allowed = KIND_SLOTS[kind]
    if not paths:
        return list(allowed)
    chosen: list[str] = []
    for raw in paths:
        prefix = "/" + raw.strip().strip("/")
        hits = [s for s in allowed if s == prefix or s.startswith(prefix + "/")]
        if not hits:
            raise TemplateError(f"{raw!r} names no {kind} template slot (allowed: {', '.join(allowed)})")
        chosen.extend(h for h in hits if h not in chosen)
    return [s for s in allowed if s in chosen]


def _get(doc: Mapping[str, Any], path: str) -> tuple[bool, Any]:
    node: Any = doc
    for part in path.strip("/").split("/"):
        if not isinstance(node, Mapping) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _set(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = path.strip("/").split("/")
    node = doc
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def slot_values(body: Mapping[str, Any]) -> dict[str, Any]:
    """slot path → value of every slot present in a (validated) body; null values are absent."""
    return {path: value for path in SLOTS for present, value in [_get(body, path)] if present and value is not None}


def _talking_cameras(spec: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        shot["camera"]
        for scene in spec.get("scenes", [])
        for shot in scene.get("shots", [])
        if shot.get("type") == "talking_head" and shot.get("camera")
    ]


def _most_common(values: Iterable[Any]) -> tuple[bool, Any]:
    counts: dict[str, tuple[int, int, Any]] = {}
    for order, value in enumerate(values):
        if value is None:
            continue
        key = _canonical(value)
        n, first, _ = counts.get(key, (0, order, value))
        counts[key] = (n + 1, first, value)
    if not counts:
        return False, None
    best = max(counts.values(), key=lambda t: (t[0], -t[1]))  # most frequent, then first seen
    return True, best[2]


def capture(spec: Mapping[str, Any], kind: str, paths: Sequence[str] | None = None) -> dict[str, Any]:
    """A template body from a version's spec (dict form): the selected slots' values. Camera and
    pacing defaults are the most common value over talking shots / scenes."""
    body: dict[str, Any] = {}
    for path in select_slots(kind, paths):
        if path.startswith("/shot_defaults/camera/"):
            name = path.rsplit("/", 1)[1]
            present, value = _most_common(c.get(name) for c in _talking_cameras(spec))
        elif path.startswith("/scene_defaults/pacing/"):
            name = path.rsplit("/", 1)[1]
            present, value = _most_common((s.get("pacing") or {}).get(name) for s in spec.get("scenes", []))
        else:
            present, value = _get(spec, path)
            if path == "/meta/platform_targets" and not value:
                present = False
            if path == "/render/outputs" and not value:
                present = False
            if path == "/brand/brand_kit_id" and value is None:
                present = False
        if present:
            _set(body, path, value)
    return body


@dataclass(frozen=True)
class Conflict:
    path: str
    values: list[dict[str, Any]]  # [{template_id, value}] in merge order
    winner: Any

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "values": self.values, "winner": self.winner}


@dataclass
class Composed:
    body: dict[str, Any]
    conflicts: list[Conflict] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)  # slot path → template id that set it


def _append(slot: Slot, before: list[Any], after: list[Any]) -> list[Any]:
    out = list(before)
    seen = {_canonical(i[slot.list_key] if slot.list_key else i) for i in out}
    for item in after:
        ident = _canonical(item[slot.list_key] if slot.list_key else item)
        if ident not in seen:
            seen.add(ident)
            out.append(item)
    return out


def compose(layers: Sequence[tuple[str, Mapping[str, Any]]]) -> Composed:
    """Deep-merges template bodies in order (`composes_from` order, then the template): later wins
    for scalars and objects, `merge: append` slots concatenate (de-duplicated). Differing values of
    a replaced slot are conflicts."""
    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    history: dict[str, list[dict[str, Any]]] = {}
    for template_id, layer in layers:
        for path, value in slot_values(validate_body(layer)).items():
            slot = SLOTS[path]
            history.setdefault(path, []).append({"template_id": template_id, "value": value})
            if path in merged and slot.merge == "append":
                merged[path] = _append(slot, merged[path], list(value))
            else:
                merged[path] = value
            sources[path] = template_id
    conflicts = [
        Conflict(path, values, merged[path])
        for path, values in history.items()
        if SLOTS[path].merge == "replace" and len({_canonical(v["value"]) for v in values}) > 1
    ]
    out: dict[str, Any] = {}
    for path in SLOTS:
        if path in merged:
            _set(out, path, merged[path])
    return Composed(body=out, conflicts=conflicts, sources=sources)


def operations(
    body: Mapping[str, Any], spec: Mapping[str, Any], *, reason: str, template_ids: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Edit operations (dict form, `parse_operations` input) that bring `spec` to the body's
    values. Slots already at the value produce nothing; append slots add only what is missing.
    The applied template ids are added to `meta.template_ids` (provenance of the change)."""
    values = slot_values(body)
    ops: list[dict[str, Any]] = []

    def changed(path: str) -> bool:
        if path not in values:
            return False
        present, current = _get(spec, path)
        return not present or _canonical(current) != _canonical(values[path])

    meta = {}
    for name in ("primary_aspect", "target_duration_s", "quality_tier"):
        if changed(f"/meta/{name}"):
            meta[name] = values[f"/meta/{name}"]
    if "/meta/platform_targets" in values:
        current = list(spec.get("meta", {}).get("platform_targets") or [])
        targets = _append(SLOTS["/meta/platform_targets"], current, list(values["/meta/platform_targets"]))
        if targets != current:
            meta["platform_targets"] = targets
    known = {str(t) for t in spec.get("meta", {}).get("template_ids") or []}
    new_ids = [str(t) for t in template_ids if str(t) not in known]
    if new_ids:
        meta["add_template_ids"] = new_ids
    if meta:
        ops.append({"op": "set_meta", **meta, "reason": reason})

    captions = {
        name: values[f"/captions/{name}"]
        for name in ("enabled", "style_id", "language", "max_words_per_line", "highlight", "placement")
        if changed(f"/captions/{name}")
    }
    if captions:
        ops.append({"op": "set_captions", "changes": captions, "reason": reason})

    brand: dict[str, Any] = {}
    if changed("/brand/brand_kit_id"):
        kit = values["/brand/brand_kit_id"]
        brand.update({"brand_kit_id": kit} if kit else {"clear_brand_kit": True})
    if changed("/brand/logo_overlay"):
        brand["logo_overlay"] = values["/brand/logo_overlay"]
    if brand:
        ops.append({"op": "set_brand", **brand, "reason": reason})

    render: dict[str, Any] = {}
    if "/render/outputs" in values:
        current = list(spec.get("render", {}).get("outputs") or [])
        outputs = _append(SLOTS["/render/outputs"], current, list(values["/render/outputs"]))
        if outputs != current:
            render["outputs"] = outputs
    if changed("/render/reframe"):
        render["reframe"] = values["/render/reframe"]
    if render:
        ops.append({"op": "set_render_outputs", **render, "reason": reason})

    if changed("/provenance/visible_label"):
        ops.append(
            {"op": "set_provenance_label", "visible_label": values["/provenance/visible_label"], "reason": reason}
        )

    cameras = _talking_cameras(spec)
    camera = {
        name: values[path]
        for path in _CAMERA
        if path in values
        for name in [path.rsplit("/", 1)[1]]
        if any(c.get(name) != values[path] for c in cameras)
    }
    if camera and cameras:
        ops.append({"op": "set_camera", **camera, "reason": reason})

    scenes = spec.get("scenes", [])
    pacing: dict[str, Any] = {}
    for path in _PACING:
        if path in values:
            name = path.rsplit("/", 1)[1]
            default = 0.0 if name == "target_wpm_delta" else None
            if any((s.get("pacing") or {}).get(name, default) != values[path] for s in scenes):
                pacing[name] = values[path]
    if pacing.get("cut_cadence", "") is None:
        pacing.pop("cut_cadence")
        pacing["clear_cut_cadence"] = True
    if pacing:
        ops.append({"op": "set_pacing", **pacing, "reason": reason})
    return ops


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def body_digest(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
