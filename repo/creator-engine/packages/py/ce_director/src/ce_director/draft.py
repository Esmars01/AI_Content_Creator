"""A mutable spec under construction. VideoSpec models are frozen (§11), so the Director builds a
plain dict, adds elements with fresh keys and validates the whole spec at checkpoints."""

from __future__ import annotations

from typing import Any

from ce_core.keys import KeyKind, new_key
from ce_core.spec.videospec import VideoSpec
from ce_core.text import Token, tokenize

__all__ = ["SpecDraft", "word_span"]


def word_span(start: tuple[str, int], end: tuple[str, int]) -> dict[str, Any]:
    return {
        "kind": "words",
        "start": {"segment_key": start[0], "word": start[1]},
        "end": {"segment_key": end[0], "word": end[1]},
    }


class SpecDraft:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self._tokens: dict[str, list[Token]] = {}  # by segment text

    # ------------------------------------------------------------------ keys
    def all_keys(self) -> set[str]:
        d = self.data
        keys: set[str] = set()
        keys.update(h["key"] for h in d.get("brief", {}).get("hook_candidates", []))
        if d.get("research"):
            keys.update(c["key"] for c in d["research"].get("claims", []))
        keys.update(c["key"] for c in d.get("cast", []))
        keys.update(p["key"] for p in d.get("products", []))
        for seg in d.get("script", {}).get("segments", []):
            keys.add(seg["key"])
            keys.update(a["key"] for a in seg.get("annotations", []))
        for scene in d.get("scenes", []):
            keys.add(scene["key"])
            acting = scene.get("acting") or {}
            keys.update(s["key"] for s in acting.get("states", []))
            keys.update(e["key"] for e in acting.get("events", []))
            for shot in scene.get("shots", []):
                keys.add(shot["key"])
                keys.update(m["key"] for m in shot.get("camera", {}).get("moves", []))
        keys.update(c["key"] for c in d.get("audio", {}).get("music", {}).get("cues", []))
        keys.update(s["key"] for s in d.get("audio", {}).get("sfx", []))
        keys.update(e["key"] for e in d.get("effects", []))
        return keys

    def new_key(self, kind: KeyKind) -> str:
        return new_key(kind, self.all_keys())

    # ------------------------------------------------------------------ script and words
    def segments(self) -> list[dict[str, Any]]:
        return list(self.data["script"]["segments"])

    def segment(self, key: str) -> dict[str, Any]:
        return next(s for s in self.data["script"]["segments"] if s["key"] == key)

    def tokens(self, segment_key: str) -> list[Token]:
        text = self.segment(segment_key)["text"]
        if text not in self._tokens:
            self._tokens[text] = tokenize(text)
        return self._tokens[text]

    def scenes(self) -> list[dict[str, Any]]:
        return sorted(self.data["scenes"], key=lambda s: s["order"])

    def scene(self, key: str) -> dict[str, Any]:
        return next(s for s in self.data["scenes"] if s["key"] == key)

    def scene_of_segment(self, segment_key: str) -> dict[str, Any] | None:
        return next((s for s in self.data["scenes"] if segment_key in s["segment_keys"]), None)

    def scene_words(self, scene_key: str) -> list[tuple[str, int]]:
        scene = self.scene(scene_key)
        return [(seg, t.index) for seg in scene["segment_keys"] for t in self.tokens(seg)]

    def position(self, scene_key: str, ref: tuple[str, int]) -> int | None:
        try:
            return self.scene_words(scene_key).index(ref)
        except ValueError:
            return None

    def ref(self, scene_key: str, position: int) -> tuple[str, int]:
        return self.scene_words(scene_key)[position]

    def span_range(self, scene_key: str, span: dict[str, Any]) -> tuple[int, int] | None:
        if span.get("kind") != "words":
            return None
        first = self.position(scene_key, (span["start"]["segment_key"], span["start"]["word"]))
        last = self.position(scene_key, (span["end"]["segment_key"], span["end"]["word"]))
        return (first, last) if first is not None and last is not None else None

    # ------------------------------------------------------------------ elements
    def add_annotation(
        self, segment_key: str, type_: str, tag: str, start: int, end: int | None = None, *, source: str
    ) -> str | None:
        """Adds an annotation unless one of the same type already covers that span."""
        seg = self.segment(segment_key)
        end = start if end is None else end
        for ann in seg["annotations"]:
            if ann["type"] == type_ and ann["span"]["start"]["word"] == start and ann["span"]["end"]["word"] == end:
                return None
        key = self.new_key(KeyKind.ANNOTATION)
        seg["annotations"].append(
            {
                "key": key,
                "type": type_,
                "tag": tag,
                "span": word_span((segment_key, start), (segment_key, end)),
                "source": source,
            }
        )
        return key

    def base_shot_at(self, scene_key: str, position: int, *, character: str | None = None) -> dict[str, Any] | None:
        for shot in self.scene(scene_key)["shots"]:
            if shot["layer"] != "base" or shot["type"] != "talking_head":
                continue
            if character is not None and shot.get("character_key") != character:
                continue
            rng = self.span_range(scene_key, shot["span"])
            if rng is not None and rng[0] <= position <= rng[1]:
                return shot
        return None

    def add_camera_move(
        self,
        scene_key: str,
        at: tuple[str, int],
        move_type: str,
        *,
        scale: float | None,
        derived_from: list[dict[str, Any]],
    ) -> str | None:
        """A camera move on the talking shot containing `at` (none twice at one word)."""
        position = self.position(scene_key, at)
        shot = self.base_shot_at(scene_key, position) if position is not None else None
        if shot is None:
            return None
        for move in shot["camera"]["moves"]:
            if (move["at"]["segment_key"], move["at"]["word"]) == at and move["type"] == move_type:
                for entry in derived_from:
                    if entry not in move["derived_from"]:
                        move["derived_from"].append(entry)
                return str(move["key"])
        key = self.new_key(KeyKind.CAMERA_MOVE)
        shot["camera"]["moves"].append(
            {
                "key": key,
                "type": move_type,
                "at": {"segment_key": at[0], "word": at[1]},
                "scale": scale,
                "transition": "cut",
                "derived_from": derived_from,
            }
        )
        return key

    def spec(self) -> VideoSpec:
        return VideoSpec.model_validate(self.data)
