"""Lock checks (§12.7, invariant I8): **locks pin spec values**. A change to a locked value in
scope is refused with the lock group named.

A lock `{group, scope}` expands through `config/vocab/edit_vocabulary.yaml`: each of the group's
SpecPath patterns, with the `[*]` selectors its scope restricts (`scope: {scene_keys: scenes}`
restricts `/scenes[*]`), expanded against the parent spec into concrete paths. The check compares
the value at every locked path before and after the edit, so it does not matter which operation
changed it. Patterns of scoped groups are expanded against the parent only (a new scene carries
no locked value yet); unscoped groups (`script`, `music`, …) also cover elements the edit adds
(a new segment changes the locked wording).

Regenerate requests are checked separately (`regenerate_refusals`): a component is refused when a
lock group that blocks it is active in scope, or when every input of its nodes is locked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ce_core.spec.paths import GLOB, PathSegment, SpecPath, SpecPathError
from ce_core.vocab import Vocabulary

__all__ = [
    "LockViolation",
    "RegenerateRefusal",
    "describe_scope",
    "expand_lock",
    "lock_applies",
    "lock_violations",
    "regenerate_refusals",
]

_MISSING = object()


@dataclass(frozen=True)
class LockViolation:
    group: str
    scope: Mapping[str, list[str] | None]
    path: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"group": self.group, "scope": dict(self.scope), "path": self.path, "message": self.message}


@dataclass(frozen=True)
class RegenerateRefusal:
    component: str
    groups: tuple[str, ...]
    message: str

    def as_dict(self) -> dict[str, Any]:
        return {"component": self.component, "groups": list(self.groups), "message": self.message}


def _scope_dict(lock: Any) -> dict[str, list[str] | None]:
    scope = lock["scope"] if isinstance(lock, Mapping) else lock.scope
    if not isinstance(scope, Mapping):
        scope = scope.model_dump(mode="json")
    return {k: (list(v) if v is not None else None) for k, v in scope.items() if k in _SCOPE_FIELDS}


def _group(lock: Any) -> str:
    return str(lock["group"] if isinstance(lock, Mapping) else lock.group)


_SCOPE_FIELDS = ("scene_keys", "character_keys", "shot_keys")


def describe_scope(scope: Mapping[str, list[str] | None]) -> str:
    parts = []
    labels = {"scene_keys": "scenes", "character_keys": "characters", "shot_keys": "shots"}
    for name in _SCOPE_FIELDS:
        keys = scope.get(name)
        if keys is not None:
            parts.append(f"{labels[name]}: {', '.join(keys) if keys else 'none'}")
    return f" ({'; '.join(parts)})" if parts else ""


def _scenes_of_shots(doc: Mapping[str, Any], shots: Iterable[str]) -> list[str]:
    wanted = set(shots)
    return [
        str(scene["key"])
        for scene in doc.get("scenes", [])
        if any(shot.get("key") in wanted for shot in scene.get("shots", []))
    ]


def _restricted(
    pattern: SpecPath, field_scope: Mapping[Any, str], scope: Mapping[str, list[str] | None], doc: Any
) -> list[SpecPath]:
    """The pattern with each scoped `[*]` replaced by the scope's keys (one path per combination)."""
    restrictions: dict[str, list[str]] = {}
    for scope_name, field in field_scope.items():
        keys = scope.get(scope_name)
        if keys is not None:
            restrictions[field] = list(keys)
    fields_in_pattern = {s.field for s in pattern.segments}
    shot_keys = scope.get("shot_keys")
    if shot_keys is not None and "shots" not in fields_in_pattern and "scenes" in fields_in_pattern:
        # a shot-scoped lock on a scene-level path (camera position): the scenes holding the shots
        restrictions.setdefault("scenes", _scenes_of_shots(doc, shot_keys))
    paths: list[tuple[PathSegment, ...]] = [()]
    for segment in pattern.segments:
        if segment.selector == GLOB and segment.field in restrictions:
            options = [PathSegment(segment.field, key) for key in restrictions[segment.field]]
        else:
            options = [segment]
        paths = [(*prefix, option) for prefix in paths for option in options]
    return [SpecPath(p) for p in paths]


def expand_lock(lock: Any, vocab: Vocabulary, doc: Any, *, extra_docs: Sequence[Any] = ()) -> list[SpecPath]:
    """Concrete SpecPaths a lock pins in `doc` (and in `extra_docs` for unscoped groups)."""
    definition = vocab.lock_groups.get(_group(lock))
    if definition is None:
        return []
    scope = _scope_dict(lock)
    out: dict[str, SpecPath] = {}
    for raw in definition.patterns:
        pattern = SpecPath.parse(raw, allow_glob=True)
        for restricted in _restricted(pattern, definition.scope, scope, doc):
            documents = [doc, *(extra_docs if not definition.scope else ())]
            for document in documents:
                for path in restricted.expand(document) if restricted.is_glob else [restricted]:
                    out.setdefault(str(path), path)
    return list(out.values())


def lock_applies(lock: Any, *, scene: str | None = None, character: str | None = None, shot: str | None = None) -> bool:
    """Whether a lock covers a scene / character / shot (an unrestricted scope covers all)."""
    scope = _scope_dict(lock)
    for name, value in (("scene_keys", scene), ("character_keys", character), ("shot_keys", shot)):
        keys = scope.get(name)
        if keys is not None and value is not None and value not in keys:
            return False
    return True


def _value(path: SpecPath, doc: Any) -> Any:
    try:
        return path.resolve(doc)
    except SpecPathError:
        return _MISSING


def lock_violations(
    before: Mapping[str, Any], after: Mapping[str, Any], locks: Iterable[Any], vocab: Vocabulary
) -> list[LockViolation]:
    """Every locked value the edit changes. `before`/`after` are spec documents (JSON form);
    `locks` are the locks in force (the parent's, minus locks the user removed in this edit)."""
    out: list[LockViolation] = []
    seen: set[tuple[str, str]] = set()
    for lock in locks:
        group = _group(lock)
        scope = _scope_dict(lock)
        for path in expand_lock(lock, vocab, before, extra_docs=[after]):
            old, new = _value(path, before), _value(path, after)
            if old == new or (group, str(path)) in seen:
                continue
            seen.add((group, str(path)))
            action = "removes" if new is _MISSING else "adds" if old is _MISSING else "changes"
            out.append(
                LockViolation(
                    group=group,
                    scope=scope,
                    path=str(path),
                    message=(
                        f"This edit {action} {path}, which the `{group}` lock pins{describe_scope(scope)}. "
                        f"Remove the {group} lock to change it."
                    ),
                )
            )
    return out


def regenerate_refusals(
    components: Iterable[str],
    locks: Iterable[Any],
    vocab: Vocabulary,
    *,
    scenes: Iterable[str | None] = (None,),
    characters: Iterable[str | None] = (None,),
    shots: Iterable[str | None] = (None,),
    seed_policy: str = "new",
) -> list[RegenerateRefusal]:
    """Refusals for a regenerate request (§12.7, §30): a component blocked by an active lock group
    in scope, or — with `seed_policy: same` — one whose every input group is locked (the re-run
    could only reproduce the same artifact). A new seed is a legitimate variation of locked inputs."""
    locks = list(locks)
    targets = [(sc, ch, sh) for sc in scenes for ch in characters for sh in shots]

    def active(group: str) -> bool:
        return any(
            _group(lock) == group and lock_applies(lock, scene=sc, character=ch, shot=sh)
            for lock in locks
            for sc, ch, sh in targets
        )

    out: list[RegenerateRefusal] = []
    for name in components:
        component = vocab.regenerate_components.get(name)
        if component is None:
            out.append(RegenerateRefusal(name, (), f"unknown regenerate component {name!r}"))
            continue
        blocking = tuple(g for g in component.blocked_by if active(g))
        if blocking:
            out.append(
                RegenerateRefusal(
                    name,
                    blocking,
                    f"Regenerating {name} is refused: the {', '.join(blocking)} lock pins it. Remove the lock first.",
                )
            )
            continue
        inputs = tuple(component.input_lock_groups)
        if seed_policy == "same" and inputs and all(active(g) for g in inputs):
            out.append(
                RegenerateRefusal(
                    name,
                    inputs,
                    f"Regenerating {name} with the same seed is refused: every input ({', '.join(inputs)}) is "
                    "locked, so nothing could change. Use a new seed or unlock an input.",
                )
            )
    return out
