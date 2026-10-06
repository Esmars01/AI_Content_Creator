"""SpecPath: key-addressed paths into a spec (§10.5, ADR 0025).

Syntax: `/field[selector]/field/...`. A selector picks one element of a list by its
identity (`key`, else `character_key`, else `preset_id`, else the value of a string list)
or one entry of a map by its key. Array indices are never used. The glob selector `[*]` is
allowed only in lock and vocabulary patterns (`allow_glob=True`).

Examples: `/cast[char_alex]/overrides/voice_version_id`,
`/scenes[scn_hook]/acting/states[st_2]/emotion/displayed`,
`/generation/seed_overrides[avatar.render:sht_1:c2:t1]`.

`to_json_pointer` renders an RFC 6901 pointer with indices for display-only diffs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel

__all__ = [
    "GLOB",
    "IDENTITY_FIELDS",
    "PathSegment",
    "SpecPath",
    "SpecPathError",
    "SpecPathPatternStr",
    "SpecPathStr",
]

GLOB = "*"
IDENTITY_FIELDS = ("key", "character_key", "preset_id")
_SEGMENT = re.compile(r"^(?P<field>[a-z_][a-z0-9_]*)(?:\[(?P<selector>[^\[\]/]+)\])?$")


class SpecPathError(ValueError):
    """Raised for malformed paths and paths that do not resolve."""


@dataclass(frozen=True, slots=True)
class PathSegment:
    field: str
    selector: str | None = None

    def __str__(self) -> str:
        return self.field if self.selector is None else f"{self.field}[{self.selector}]"

    def matches(self, other: PathSegment) -> bool:
        """Whether this (possibly glob) segment matches a concrete segment."""
        if self.field != other.field:
            return False
        if self.selector == GLOB:
            return other.selector is not None
        return self.selector == other.selector


@dataclass(frozen=True, slots=True)
class SpecPath:
    segments: tuple[PathSegment, ...]

    # ------------------------------------------------------------------ construction
    @classmethod
    def parse(cls, text: str, *, allow_glob: bool = False) -> SpecPath:
        if not isinstance(text, str) or not text.startswith("/"):
            raise SpecPathError(f"SpecPath must start with '/': {text!r}")
        if text == "/":
            return cls(())
        segments: list[PathSegment] = []
        for raw in text[1:].split("/"):
            match = _SEGMENT.match(raw)
            if not match:
                raise SpecPathError(f"invalid SpecPath segment {raw!r} in {text!r}")
            selector = match.group("selector")
            if selector is not None and selector.isdigit():
                raise SpecPathError(f"array indices are not allowed in SpecPaths ({text!r}); use the element key")
            if selector == GLOB and not allow_glob:
                raise SpecPathError(f"'[*]' is only allowed in lock and vocabulary patterns: {text!r}")
            segments.append(PathSegment(match.group("field"), selector))
        return cls(tuple(segments))

    def child(self, field: str, selector: str | None = None) -> SpecPath:
        return SpecPath((*self.segments, PathSegment(field, selector)))

    @property
    def parent(self) -> SpecPath:
        if not self.segments:
            raise SpecPathError("the root path has no parent")
        return SpecPath(self.segments[:-1])

    def __str__(self) -> str:
        return "/" + "/".join(str(s) for s in self.segments)

    @property
    def is_glob(self) -> bool:
        return any(s.selector == GLOB for s in self.segments)

    # ------------------------------------------------------------------ pattern logic
    def matches(self, path: SpecPath) -> bool:
        """Whether this pattern addresses exactly `path`."""
        return len(self.segments) == len(path.segments) and all(
            p.matches(s) for p, s in zip(self.segments, path.segments, strict=True)
        )

    def covers(self, path: SpecPath) -> bool:
        """Whether this pattern addresses `path` or one of its ancestors (so a change at `path` is inside it)."""
        if len(self.segments) > len(path.segments):
            return False
        return all(p.matches(s) for p, s in zip(self.segments, path.segments[: len(self.segments)], strict=True))

    def overlaps(self, path: SpecPath) -> bool:
        """Whether a change at `path` and the region of this pattern intersect (either contains the other)."""
        n = min(len(self.segments), len(path.segments))
        for p, s in zip(self.segments[:n], path.segments[:n], strict=True):
            if p.field != s.field:
                return False
            if GLOB in (p.selector, s.selector):
                continue
            if p.selector is None or s.selector is None:
                # A path without a selector addresses the whole list or map.
                continue
            if p.selector != s.selector:
                return False
        return True

    # ------------------------------------------------------------------ resolution
    def resolve(self, document: Any) -> Any:
        """The value at this path in a spec (model or plain JSON-like data)."""
        if self.is_glob:
            raise SpecPathError(f"cannot resolve a glob pattern to one value: {self}")
        node = _as_data(document)
        for segment in self.segments:
            node = _step(node, segment, str(self))
        return node

    def exists(self, document: Any) -> bool:
        try:
            self.resolve(document)
        except SpecPathError:
            return False
        return True

    def expand(self, document: Any) -> list[SpecPath]:
        """All concrete paths in `document` that this (glob) pattern matches."""
        results: list[SpecPath] = []
        _expand(_as_data(document), list(self.segments), SpecPath(()), results)
        return results

    def to_json_pointer(self, document: Any) -> str:
        """Display-only RFC 6901 rendering with array indices for this document."""
        if self.is_glob:
            raise SpecPathError("glob patterns have no JSON Pointer form")
        node = _as_data(document)
        parts: list[str] = []
        for segment in self.segments:
            if not isinstance(node, Mapping) or segment.field not in node:
                raise SpecPathError(f"{self}: field {segment.field!r} not found")
            parts.append(segment.field)
            node = node[segment.field]
            if segment.selector is not None:
                index, node = _select(node, segment.selector, str(self))
                parts.append(str(index))
        return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in parts)


def _as_data(document: Any) -> Any:
    if isinstance(document, BaseModel):
        return document.model_dump(mode="json", by_alias=True)
    return document


def _identity(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for name in IDENTITY_FIELDS:
            value = item.get(name)
            if isinstance(value, str):
                return value
    return None


def _select(container: Any, selector: str, path: str) -> tuple[str | int, Any]:
    if isinstance(container, Mapping):
        if selector not in container:
            raise SpecPathError(f"{path}: no entry {selector!r}")
        return selector, container[selector]
    if isinstance(container, Sequence) and not isinstance(container, str):
        for index, item in enumerate(container):
            if _identity(item) == selector:
                return index, item
        raise SpecPathError(f"{path}: no element with key {selector!r}")
    raise SpecPathError(f"{path}: selector [{selector}] applied to a scalar")


def _step(node: Any, segment: PathSegment, path: str) -> Any:
    if not isinstance(node, Mapping) or segment.field not in node:
        raise SpecPathError(f"{path}: field {segment.field!r} not found")
    value = node[segment.field]
    if segment.selector is None:
        return value
    return _select(value, segment.selector, path)[1]


def _children(container: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(container, Mapping):
        yield from ((str(k), v) for k, v in container.items())
    elif isinstance(container, Sequence) and not isinstance(container, str):
        for item in container:
            identity = _identity(item)
            if identity is not None:
                yield identity, item


def _expand(node: Any, remaining: list[PathSegment], prefix: SpecPath, out: list[SpecPath]) -> None:
    if not remaining:
        out.append(prefix)
        return
    segment, rest = remaining[0], remaining[1:]
    if not isinstance(node, Mapping) or segment.field not in node:
        return
    value = node[segment.field]
    if segment.selector is None:
        _expand(value, rest, prefix.child(segment.field), out)
    elif segment.selector == GLOB:
        for identity, item in _children(value):
            _expand(item, rest, prefix.child(segment.field, identity), out)
    else:
        try:
            _, item = _select(value, segment.selector, str(prefix))
        except SpecPathError:
            return
        _expand(item, rest, prefix.child(segment.field, segment.selector), out)


def _validate_path(value: str) -> str:
    SpecPath.parse(value)
    return value


def _validate_pattern(value: str) -> str:
    SpecPath.parse(value, allow_glob=True)
    return value


SpecPathStr = Annotated[str, AfterValidator(_validate_path)]
"""A concrete SpecPath as a string field (no globs)."""

SpecPathPatternStr = Annotated[str, AfterValidator(_validate_pattern)]
"""A SpecPath pattern as a string field (`[*]` allowed): lock and vocabulary patterns only."""
