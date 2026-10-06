"""Anchor rebase for script edits (ADR 0004): time is anchored to words, so when a segment's text
changes every word anchor into that segment is moved through a token alignment of the old and
new wording (difflib over case-folded canonical tokens).

Rules, per segment:
- a word that survives maps to its new index;
- a span **start** moves to the first surviving word at or after it (0 stays 0);
- a span **end** moves to just before the next surviving word after it (the last word stays
  last), so words inserted between two adjacent spans join the earlier one and tiling families
  (acting states, base shots) keep tiling;
- a point anchor (`at`) moves to the nearest surviving word at or after it, else before it;
- an element whose span lost every word is dropped and reported.

The impact preview reports moved and dropped anchors (`RebaseReport`).
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass, field
from typing import Any

from ce_core.text import tokenize

__all__ = ["RebaseReport", "rebase_segment", "word_map"]


@dataclass
class RebaseReport:
    moved: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"moved": self.moved, "dropped": self.dropped}


def word_map(old_text: str, new_text: str) -> tuple[list[int | None], int]:
    """For each old word, its index in the new text (None when deleted); and the new word count."""
    old = [t.text.casefold() for t in tokenize(old_text)]
    new = [t.text.casefold() for t in tokenize(new_text)]
    mapping: list[int | None] = [None] * len(old)
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            mapping[block.a + offset] = block.b + offset
    return mapping, len(new)


@dataclass(frozen=True)
class _Map:
    mapping: list[int | None]
    count: int

    def _next(self, old: int) -> int | None:
        for j in range(max(old, 0), len(self.mapping)):
            if self.mapping[j] is not None:
                return self.mapping[j]
        return None

    def start(self, old: int) -> int | None:
        if old <= 0:
            return 0 if self.count else None
        return self._next(old)

    def end(self, old: int) -> int | None:
        if old >= len(self.mapping) - 1:
            return self.count - 1 if self.count else None
        following = self._next(old + 1)
        return (following - 1) if following is not None else self.count - 1

    def point(self, old: int) -> int | None:
        found = self._next(old)
        if found is not None:
            return found
        for j in range(min(old, len(self.mapping) - 1), -1, -1):
            if self.mapping[j] is not None:
                return self.mapping[j]
        return None


def _is_ref(value: Any, segment_key: str) -> bool:
    return isinstance(value, Mapping) and value.get("segment_key") == segment_key and isinstance(value.get("word"), int)


def _is_span(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("kind") == "words"
        and isinstance(value.get("start"), Mapping)
        and isinstance(value.get("end"), Mapping)
    )


def _identity(item: Any) -> str | None:
    if isinstance(item, Mapping):
        for name in ("key", "character_key", "preset_id"):
            value = item.get(name)
            if isinstance(value, str):
                return value
    return None


def rebase_segment(
    document: Mapping[str, Any], segment_key: str, old_text: str, new_text: str
) -> tuple[dict[str, Any], RebaseReport]:
    """The spec document with every anchor into `segment_key` rebased onto `new_text` (the
    segment's text itself is not changed here) and the report of moved and dropped anchors."""
    mapping, count = word_map(old_text, new_text)
    m = _Map(mapping, count)
    doc = copy.deepcopy(dict(document))
    report = RebaseReport()
    drops: list[tuple[MutableSequence[Any], Any, str]] = []

    def visit(node: Any, path: str, owner: tuple[MutableSequence[Any], Any, str] | None) -> None:
        if isinstance(node, MutableMapping):
            if _is_span(node):
                start, end = node["start"], node["end"]
                new_start, new_end = start.get("word"), end.get("word")
                if _is_ref(start, segment_key):
                    new_start = m.start(int(start["word"]))
                if _is_ref(end, segment_key):
                    new_end = m.end(int(end["word"]))
                same_segment = start.get("segment_key") == end.get("segment_key")
                vanished = (
                    new_start is None
                    or new_end is None
                    or (same_segment and start.get("segment_key") == segment_key and new_start > new_end)
                )
                if vanished:
                    if owner is not None:
                        drops.append(owner)
                    return
                if _is_ref(start, segment_key) and new_start != start["word"]:
                    report.moved.append({"path": f"{path}/start", "from": start["word"], "to": new_start})
                    start["word"] = new_start
                if _is_ref(end, segment_key) and new_end != end["word"]:
                    report.moved.append({"path": f"{path}/end", "from": end["word"], "to": new_end})
                    end["word"] = new_end
                return
            if _is_ref(node, segment_key):
                moved = m.point(int(node["word"]))
                if moved is None:
                    if owner is not None:
                        drops.append(owner)
                    return
                if moved != node["word"]:
                    report.moved.append({"path": path, "from": node["word"], "to": moved})
                    node["word"] = moved
                return
            for key, value in node.items():
                visit(value, f"{path}/{key}", owner)
            return
        if isinstance(node, MutableSequence):
            for item in list(node):
                identity = _identity(item)
                child_path = f"{path}[{identity}]" if identity else path
                visit(item, child_path, (node, item, child_path) if identity else owner)

    for key, value in doc.items():
        visit(value, f"/{key}", None)
    for container, item, path in drops:
        if any(entry is item for entry in container):
            for index, entry in enumerate(container):
                if entry is item:
                    del container[index]
                    break
            report.dropped.append({"path": path, "reason": "every word it was anchored to was deleted"})
    return doc, report
