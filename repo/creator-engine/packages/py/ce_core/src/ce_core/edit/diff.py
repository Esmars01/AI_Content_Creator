"""Structured spec diff (§12.8 compare, §28 proposals): key-addressed, so a reordered list of keyed
elements is not a change and every entry names a SpecPath.

Lists whose items all carry an identity (`key`, `character_key`, `preset_id`) are compared by
key; other lists (word spans' fields, string lists such as `platform_targets`) compare as values.
Version identity fields (`video_id`, `version_id`, `parent_version_id`) are ignored.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

__all__ = ["IGNORED", "MAP_FIELDS", "DiffEntry", "area_of", "spec_diff"]

IGNORED = frozenset({"video_id", "version_id", "parent_version_id"})
# Spec fields that are maps (entries are addressed `field[key]`, like keyed list elements).
MAP_FIELDS = frozenset({"element_states", "seed_overrides", "engine_hints", "review_state", "params"})
# Keyed lists whose order is meaningful (validated against the scene order): a reorder is a change.
ORDERED_LISTS = frozenset({"segments", "segment_keys"})


@dataclass(frozen=True)
class DiffEntry:
    path: str
    kind: Literal["added", "removed", "changed"]
    before: Any = None
    after: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "before": self.before, "after": self.after}


def _identity(item: Any) -> str | None:
    if isinstance(item, Mapping):
        for name in ("key", "character_key", "preset_id"):
            value = item.get(name)
            if isinstance(value, str):
                return value
    return None


def _keyed(items: Sequence[Any]) -> bool:
    return bool(items) and all(_identity(i) is not None for i in items)


def _walk(a: Any, b: Any, path: str, out: list[DiffEntry]) -> None:
    if a == b:
        return
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        is_map = path.rsplit("/", 1)[-1] in MAP_FIELDS
        for key in list(dict.fromkeys([*a.keys(), *b.keys()])):
            if not path and key in IGNORED:
                continue
            child = f"{path}[{key}]" if is_map else f"{path}/{key}"
            if key not in a:
                out.append(DiffEntry(child, "added", None, b[key]))
            elif key not in b:
                out.append(DiffEntry(child, "removed", a[key], None))
            else:
                _walk(a[key], b[key], child, out)
        return
    if (
        isinstance(a, Sequence)
        and isinstance(b, Sequence)
        and not isinstance(a, str)
        and not isinstance(b, str)
        and (_keyed(a) or _keyed(b))
        and all(_identity(i) is not None for i in [*a, *b])
    ):
        left = {_identity(i): i for i in a}
        right = {_identity(i): i for i in b}
        common_left = [k for k in left if k in right]
        common_right = [k for k in right if k in left]
        if common_left != common_right and path.rsplit("/", 1)[-1] in ORDERED_LISTS:
            out.append(DiffEntry(path, "changed", list(a), list(b)))  # a reorder is one change
            return
        field, _, _ = path.rpartition("/")
        name = path[len(field) + 1 :]
        for key in list(dict.fromkeys([*left.keys(), *right.keys()])):
            child = f"{field}/{name}[{key}]"
            if key not in left:
                out.append(DiffEntry(child, "added", None, right[key]))
            elif key not in right:
                out.append(DiffEntry(child, "removed", left[key], None))
            else:
                _walk(left[key], right[key], child, out)
        return
    out.append(DiffEntry(path or "/", "changed", a, b))


def spec_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[DiffEntry]:
    """Every difference between two spec documents (JSON form)."""
    out: list[DiffEntry] = []
    _walk(dict(before), dict(after), "", out)
    return out


_AREAS = (
    ("/script", "script"),
    ("/cast", "cast"),
    ("/intent", "intent"),
    ("/meta", "meta"),
    ("/audio", "audio"),
    ("/captions", "captions"),
    ("/render", "output"),
    ("/generation", "generation"),
    ("/locks", "locks"),
    ("/memory", "memory"),
)


def area_of(path: str) -> str:
    """A coarse area for grouping diffs in the UI (acting, world, camera, script, …)."""
    if path.startswith("/scenes"):
        for needle, area in (
            ("/acting", "acting"),
            ("/world", "world"),
            ("/camera", "camera"),
            ("/intent", "intent"),
            ("/pacing", "pacing"),
            ("/cast", "wardrobe"),
            ("/shots", "shots"),
        ):
            if needle in path:
                return area
        return "scenes"
    for prefix, area in _AREAS:
        if path.startswith(prefix):
            return area
    return "other"
