"""SpecPatch: key-addressed changes to a spec (ADR 0025). Code produces it from `EditOperation`s;
nothing else writes specs.

Each change names a concrete SpecPath:
- `set` replaces a field, a map entry (created when absent) or a keyed list element;
- `add` inserts a keyed list element that does not exist yet (appended, or after the sibling
  named in `after`), a map entry, or a value into a list of strings (`hide_elements[el_x]`);
- `remove` deletes a keyed list element or map entry, or sets an optional field to null.

`apply_patch` works on the JSON form of a spec and never mutates its input; the caller validates
the result as a `VideoSpec` (and with `validate_spec`).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping, MutableSequence
from typing import Any, Literal

from pydantic import Field

from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPath, SpecPathError, SpecPathStr

__all__ = ["PatchError", "PatchOp", "SpecPatch", "apply_patch", "patch_from_diff"]


class PatchError(ValueError):
    """A patch change does not apply to the document (missing parent, duplicate key, …)."""


class PatchOp(SpecModel):
    op: Literal["set", "add", "remove"]
    path: SpecPathStr
    value: Any = None
    after: str | None = Field(default=None, description="add: insert after the sibling with this key")


class SpecPatch(SpecModel):
    ops: list[PatchOp] = Field(default_factory=list)

    def paths(self) -> list[str]:
        return [o.path for o in self.ops]

    def __bool__(self) -> bool:
        return bool(self.ops)

    def __add__(self, other: SpecPatch) -> SpecPatch:
        return SpecPatch(ops=[*self.ops, *other.ops])


def _identity(item: Any) -> str | None:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        for name in ("key", "character_key", "preset_id"):
            value = item.get(name)
            if isinstance(value, str):
                return value
    return None


def _container(doc: Any, path: SpecPath) -> Any:
    """The value the last segment's field lives in (a mapping)."""
    node = doc
    for segment in path.segments[:-1]:
        if not isinstance(node, Mapping) or segment.field not in node:
            raise PatchError(f"{path}: field {segment.field!r} not found")
        node = node[segment.field]
        if segment.selector is not None:
            node = _child(node, segment.selector, path)
    if not isinstance(node, MutableMapping):
        raise PatchError(f"{path}: parent is not an object")
    return node


def _child(container: Any, selector: str, path: SpecPath) -> Any:
    if isinstance(container, Mapping):
        if selector not in container:
            raise PatchError(f"{path}: no entry {selector!r}")
        return container[selector]
    if isinstance(container, list):
        for item in container:
            if _identity(item) == selector:
                return item
        raise PatchError(f"{path}: no element with key {selector!r}")
    raise PatchError(f"{path}: selector [{selector}] applied to a scalar")


def _index(items: MutableSequence[Any], selector: str) -> int | None:
    for index, item in enumerate(items):
        if _identity(item) == selector:
            return index
    return None


def _apply_one(doc: Any, change: PatchOp) -> None:
    path = SpecPath.parse(change.path)
    if not path.segments:
        raise PatchError("the root cannot be patched")
    last = path.segments[-1]
    parent = _container(doc, path)
    value = copy.deepcopy(change.value)
    if last.selector is None:
        if change.op == "remove":
            if last.field not in parent:
                raise PatchError(f"{path}: field {last.field!r} not found")
            parent[last.field] = None
            return
        if change.op == "add":
            raise PatchError(f"{path}: `add` needs a keyed path (field[key])")
        parent[last.field] = value
        return
    if last.field not in parent:
        if change.op == "remove":
            raise PatchError(f"{path}: field {last.field!r} not found")
        parent[last.field] = {} if not isinstance(value, Mapping) or _identity(value) is None else []
    target = parent[last.field]
    if target is None:
        if change.op == "remove":
            raise PatchError(f"{path}: {last.field} is empty")
        target = parent[last.field] = []
    if isinstance(target, MutableMapping):
        if change.op == "remove":
            if last.selector not in target:
                raise PatchError(f"{path}: no entry {last.selector!r}")
            del target[last.selector]
        elif change.op == "add" and last.selector in target:
            raise PatchError(f"{path}: entry {last.selector!r} exists")
        else:
            target[last.selector] = value
        return
    if not isinstance(target, MutableSequence):
        raise PatchError(f"{path}: {last.field} is neither a list nor a map")
    index = _index(target, last.selector)
    if change.op == "remove":
        if index is None:
            raise PatchError(f"{path}: no element with key {last.selector!r}")
        del target[index]
        return
    if change.op == "set":
        if index is None:
            raise PatchError(f"{path}: no element with key {last.selector!r} (use add)")
        if _identity(value) != last.selector:
            raise PatchError(f"{path}: the new element's key must stay {last.selector!r}")
        target[index] = value
        return
    if index is not None:
        raise PatchError(f"{path}: an element with key {last.selector!r} exists")
    if _identity(value) != last.selector:
        raise PatchError(f"{path}: the added element's key must be {last.selector!r}")
    if change.after is not None:
        position = _index(target, change.after)
        if position is None:
            raise PatchError(f"{path}: no sibling {change.after!r} to insert after")
        target.insert(position + 1, value)
    else:
        target.append(value)


def apply_patch(document: Mapping[str, Any], patch: SpecPatch) -> dict[str, Any]:
    """The patched copy of a spec's JSON form."""
    doc = copy.deepcopy(dict(document))
    for change in patch.ops:
        try:
            _apply_one(doc, change)
        except SpecPathError as exc:
            raise PatchError(str(exc)) from exc
    return doc


def _siblings(document: Mapping[str, Any], path: SpecPath) -> list[str]:
    """Keys of the list a keyed path's element lives in (in `document`)."""
    try:
        container = SpecPath(path.segments[:-1]).resolve(document) if len(path.segments) > 1 else document
    except SpecPathError:
        return []
    last = path.segments[-1]
    values = container.get(last.field) if isinstance(container, Mapping) else None
    if not isinstance(values, list):
        return []
    return [k for k in (_identity(v) for v in values) if k is not None]


def patch_from_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> SpecPatch:
    """The SpecPatch that turns `before` into `after` (both spec documents): `set` for changed
    values, `add` for new keyed elements and map entries (after their preceding sibling), `remove`
    for deleted ones. Applying it to `before` reproduces `after` up to list order of new elements."""
    from ce_core.edit.diff import spec_diff

    ops: list[PatchOp] = []
    for entry in spec_diff(before, after):
        path = SpecPath.parse(entry.path)
        keyed = bool(path.segments) and path.segments[-1].selector is not None
        if entry.kind == "changed" or (entry.kind == "added" and not keyed):
            ops.append(PatchOp(op="set", path=entry.path, value=entry.after))
        elif entry.kind == "removed":
            ops.append(PatchOp(op="remove", path=entry.path) if keyed else PatchOp(op="set", path=entry.path))
        else:
            siblings = _siblings(after, path)
            key = path.segments[-1].selector
            index = siblings.index(key) if key in siblings else -1
            previous = siblings[index - 1] if index > 0 else None
            ops.append(PatchOp(op="add", path=entry.path, value=entry.after, after=previous))
    return SpecPatch(ops=_order(ops))


def _order(ops: list[PatchOp]) -> list[PatchOp]:
    """Removals first (a key may be reused after a removal), then sets, then additions."""
    rank = {"remove": 0, "set": 1, "add": 2}
    return sorted(ops, key=lambda o: rank[o.op])
