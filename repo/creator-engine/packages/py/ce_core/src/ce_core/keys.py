"""Stable, prefixed string keys for spec elements and World DNA elements (§10.5).

A key is generated once and preserved across versions. Projection tables store keys in
`*_key` columns. Array indices are never used to address spec elements (see `SpecPath`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator

__all__ = [
    "AnnotationKey",
    "AssetKey",
    "CameraMoveKey",
    "CameraPositionKey",
    "CharacterKey",
    "ClaimKey",
    "EffectKey",
    "ElementKey",
    "EventKey",
    "HookKey",
    "KeyKind",
    "MusicCueKey",
    "ProductKey",
    "SceneKey",
    "SegmentKey",
    "SfxKey",
    "ShotKey",
    "StateKey",
    "TakeKey",
    "ZoneKey",
    "ZoomKey",
    "key_pattern",
    "new_key",
    "take_key",
    "validate_key",
]


class KeyKind(StrEnum):
    """Each key kind and its prefix."""

    CHARACTER = "char_"
    SEGMENT = "seg_"
    ANNOTATION = "an_"
    CLAIM = "clm_"
    HOOK = "hk_"
    SCENE = "scn_"
    SHOT = "sht_"
    TAKE = "tk_"
    CAMERA_MOVE = "mv_"
    STATE = "st_"
    EVENT = "ev_"
    MUSIC_CUE = "mc_"
    SFX = "sfx_"
    EFFECT = "fx_"
    ZOOM = "zm_"
    # World DNA elements (§10.5).
    ELEMENT = "el_"
    CAMERA_POSITION = "cam_"
    ZONE = "zone_"
    # Not named in §10.5; chosen in Phase 1 to complete the convention (docs/DECISIONS.md D13).
    PRODUCT = "prd_"
    ASSET = "as_"


_BODY = r"[a-z0-9]+(?:_[a-z0-9]+)*"
_PATTERNS = {kind: re.compile(rf"^{re.escape(kind.value)}{_BODY}$") for kind in KeyKind}
MAX_KEY_LENGTH = 64


def key_pattern(kind: KeyKind) -> str:
    """The regular expression a key of this kind must match (also used in JSON Schema)."""
    return _PATTERNS[kind].pattern


def validate_key(kind: KeyKind, value: str) -> str:
    if len(value) > MAX_KEY_LENGTH or not _PATTERNS[kind].match(value):
        raise ValueError(f"invalid {kind.name.lower()} key {value!r}: expected {kind.value}<lowercase words>")
    return value


def new_key(kind: KeyKind, existing: Iterable[str] = ()) -> str:
    """The next free numbered key of this kind (`seg_1`, `seg_2`, …), never reusing one in `existing`."""
    used = set(existing)
    highest = 0
    for key in used:
        if key.startswith(kind.value):
            suffix = key[len(kind.value) :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    candidate = highest + 1
    while f"{kind.value}{candidate}" in used:
        candidate += 1
    return f"{kind.value}{candidate}"


def take_key(index: int) -> str:
    """Take keys derive from the take index, so changing `takes.count` never renames takes (§10.5)."""
    if index < 1:
        raise ValueError("take indices start at 1")
    return f"{KeyKind.TAKE.value}{index}"


def _validator(kind: KeyKind) -> AfterValidator:
    return AfterValidator(lambda value: validate_key(kind, value))


CharacterKey = Annotated[str, _validator(KeyKind.CHARACTER)]
SegmentKey = Annotated[str, _validator(KeyKind.SEGMENT)]
AnnotationKey = Annotated[str, _validator(KeyKind.ANNOTATION)]
ClaimKey = Annotated[str, _validator(KeyKind.CLAIM)]
HookKey = Annotated[str, _validator(KeyKind.HOOK)]
SceneKey = Annotated[str, _validator(KeyKind.SCENE)]
ShotKey = Annotated[str, _validator(KeyKind.SHOT)]
TakeKey = Annotated[str, _validator(KeyKind.TAKE)]
CameraMoveKey = Annotated[str, _validator(KeyKind.CAMERA_MOVE)]
StateKey = Annotated[str, _validator(KeyKind.STATE)]
EventKey = Annotated[str, _validator(KeyKind.EVENT)]
MusicCueKey = Annotated[str, _validator(KeyKind.MUSIC_CUE)]
SfxKey = Annotated[str, _validator(KeyKind.SFX)]
EffectKey = Annotated[str, _validator(KeyKind.EFFECT)]
ZoomKey = Annotated[str, _validator(KeyKind.ZOOM)]
ElementKey = Annotated[str, _validator(KeyKind.ELEMENT)]
CameraPositionKey = Annotated[str, _validator(KeyKind.CAMERA_POSITION)]
ZoneKey = Annotated[str, _validator(KeyKind.ZONE)]
ProductKey = Annotated[str, _validator(KeyKind.PRODUCT)]
AssetKey = Annotated[str, _validator(KeyKind.ASSET)]
