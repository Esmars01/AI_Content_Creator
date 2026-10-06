"""Word geometry of a scene: the order of its words, word spans as index ranges, the acting states
that cover a word (with `carry` resolved), and the paths of acting items (§15.3, ADR 0004)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ce_core.spec.acting import ActingState
from ce_core.spec.anchors import DurationSpan, WordRef, WordSpan
from ce_core.spec.videospec import Scene, VideoSpec
from ce_core.text.tokenize import tokenize

__all__ = [
    "ResolvedState",
    "SceneWords",
    "annotation_path",
    "covering",
    "event_path",
    "resolved_states",
    "state_path",
]

Word = tuple[str, int]


def state_path(scene_key: str, state_key: str) -> str:
    return f"/scenes[{scene_key}]/acting/states[{state_key}]"


def event_path(scene_key: str, event_key: str) -> str:
    return f"/scenes[{scene_key}]/acting/events[{event_key}]"


def annotation_path(segment_key: str, annotation_key: str) -> str:
    return f"/script/segments[{segment_key}]/annotations[{annotation_key}]"


@dataclass(frozen=True)
class SceneWords:
    """The scene's words in speaking order: `(segment_key, word)` → position 0..n-1."""

    order: tuple[Word, ...]
    index: dict[Word, int]
    segment_lengths: dict[str, int]

    @classmethod
    def of(cls, spec: VideoSpec, scene: Scene) -> SceneWords:
        order: list[Word] = []
        lengths: dict[str, int] = {}
        for key in scene.segment_keys:
            tokens = tokenize(spec.script.segment(key).text)
            lengths[key] = len(tokens)
            order += [(key, t.index) for t in tokens]
        return cls(tuple(order), {w: i for i, w in enumerate(order)}, lengths)

    @property
    def count(self) -> int:
        return len(self.order)

    def position(self, ref: WordRef) -> int | None:
        return self.index.get((ref.segment_key, ref.word))

    def range(self, span: WordSpan | DurationSpan | None) -> tuple[int, int] | None:
        """Inclusive word positions of a word span; None for duration spans or unknown words."""
        if not isinstance(span, WordSpan):
            return None
        a, b = self.position(span.start), self.position(span.end)
        if a is None or b is None or b < a:
            return None
        return a, b

    def words(self, span: WordSpan | DurationSpan | None) -> list[Word]:
        rng = self.range(span)
        return [] if rng is None else list(self.order[rng[0] : rng[1] + 1])

    def segment_range(self, segment_key: str) -> tuple[int, int] | None:
        positions = [i for i, (seg, _) in enumerate(self.order) if seg == segment_key]
        return (positions[0], positions[-1]) if positions else None

    def is_segment_start(self, ref: WordRef) -> bool:
        return ref.word == 0

    def span(self, first: int, last: int) -> WordSpan:
        a, b = self.order[first], self.order[last]
        return WordSpan(start=WordRef(segment_key=a[0], word=a[1]), end=WordRef(segment_key=b[0], word=b[1]))


@dataclass(frozen=True)
class ResolvedState:
    """An acting state with `carry` applied: the values of the last non-carried state of the same
    character, the span, key, source, priority and transition of its own."""

    state: ActingState
    values: ActingState
    first: int | None
    last: int | None

    @property
    def key(self) -> str:
        return self.state.key

    def covers(self, position: int) -> bool:
        return self.first is not None and self.last is not None and self.first <= position <= self.last


def resolved_states(scene: Scene, words: SceneWords, character: str | None = None) -> list[ResolvedState]:
    """States in speaking order per character (word-anchored first, by start; duration states keep
    their authored order after the word states of their character)."""
    if scene.acting is None:
        return []
    out: list[ResolvedState] = []
    by_character: dict[str, list[ActingState]] = {}
    for state in scene.acting.states:
        if character is not None and state.character_key != character:
            continue
        by_character.setdefault(state.character_key, []).append(state)
    for states in by_character.values():

        def order(state: ActingState) -> tuple[int, int]:
            rng = words.range(state.span)
            return (0, rng[0]) if rng else (1, 0)

        previous: ActingState | None = None
        for state in sorted(states, key=order):
            values = state
            if state.carry and previous is not None:
                values = previous.model_copy(
                    update={
                        "key": state.key,
                        "source": state.source,
                        "span": state.span,
                        "carry": True,
                        "priority": state.priority,
                        "transition_in": state.transition_in,
                        "confidence_delta": state.confidence_delta,
                        "out_of_character": state.out_of_character or previous.out_of_character,
                    }
                )
            elif not state.carry:
                previous = state
            rng = words.range(state.span)
            out.append(ResolvedState(state, values, rng[0] if rng else None, rng[1] if rng else None))
    return out


def covering(states: Iterable[ResolvedState], first: int, last: int) -> ResolvedState | None:
    """The state covering the most words of [first, last] (ties: the earlier state)."""
    best: tuple[int, int, ResolvedState] | None = None
    for state in states:
        if state.first is None or state.last is None:
            continue
        overlap = min(last, state.last) - max(first, state.first) + 1
        if overlap <= 0:
            continue
        candidate = (overlap, -state.first, state)
        if best is None or candidate[:2] > best[:2]:
            best = candidate
    return best[2] if best else None
