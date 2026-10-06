"""The repetition guard (§18.6, `config/memory.yaml` `repetition`).

History comes from the creator's usage log, one entry per **distinct video** (the latest event
of each video), newest first. Hooks compare by keyword/character-n-gram similarity and, since
Phase 12, by embedding cosine against hooks embedded by the same model; phrases by word-n-gram
overlap:

- hooks: similarity above `max_similarity` to a hook of the last N videos → rejected;
- phrases: n-gram overlap with the last N videos above `max_overlap` → flagged; signature phrases
  (`speech_habit.recurring_phrase`) are exempt up to their `max_per_video`;
- emotional arcs: sequence similarity above `max_similarity` → flagged unless the strategy pack
  declares a signature format;
- visual patterns: the same shot-type/camera sequence with the same world camera position, time of
  day and wardrobe as the previous video → flagged unless pinned;
- worlds: reuse is continuity, never repetition.
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from ce_config.schemas import RepetitionConfig

from ce_memory.text import cosine, ngram_fingerprints, normalize, similarity, words

__all__ = ["RepetitionFinding", "RepetitionGuard", "UsageEntry"]


@dataclass(frozen=True)
class UsageEntry:
    video_id: UUID
    hooks: tuple[str, ...] = ()
    phrase_fingerprints: frozenset[str] = frozenset()
    arc: tuple[str, ...] = ()
    visual: dict[str, Any] = field(default_factory=dict)
    hook_vector: tuple[float, ...] | None = None  # the selected hook's embedding (Phase 12)
    hook_model: str | None = None


@dataclass(frozen=True)
class RepetitionFinding:
    check: Literal["hook", "phrase", "arc", "visual"]
    severity: Literal["reject", "flag"]
    subject: str
    score: float
    video_id: UUID | None
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "subject": self.subject,
            "score": self.score,
            "video_id": str(self.video_id) if self.video_id else None,
            "detail": self.detail,
        }


def _arc_similarity(a: Sequence[str], b: Sequence[str]) -> float:
    if not a or not b:
        return 0.0
    return round(difflib.SequenceMatcher(a=list(a), b=list(b), autojunk=False).ratio(), 4)


class RepetitionGuard:
    def __init__(self, config: RepetitionConfig) -> None:
        self.config = config

    def hooks(
        self,
        candidates: Sequence[str],
        history: Sequence[UsageEntry],
        *,
        vectors: Sequence[Sequence[float]] | None = None,
        model: str | None = None,
        max_cosine: float | None = None,
    ) -> list[RepetitionFinding]:
        """Lexical similarity (keywords, character trigrams) against every recent hook, and — when
        the candidates were embedded (`vectors`, `model`) — cosine similarity against recent hooks
        embedded by the **same** model (Phase 12). Either above its threshold rejects the hook."""
        window = history[: self.config.hooks.window_videos]
        limit = self.config.hooks.max_similarity or 0.82
        cos_limit = max_cosine if max_cosine is not None else 0.9
        out: list[RepetitionFinding] = []
        for index, hook in enumerate(candidates):
            best = max(
                ((similarity(hook, old), entry.video_id, old) for entry in window for old in entry.hooks),
                default=(0.0, None, ""),
                key=lambda x: x[0],
            )
            if best[0] > limit:
                out.append(
                    RepetitionFinding(
                        "hook", "reject", hook, best[0], best[1], f"too close to a recent hook: {best[2]!r}"
                    )
                )
                continue
            vector = vectors[index] if vectors is not None and index < len(vectors) else None
            if vector is None or not model:
                continue
            semantic = max(
                (
                    (round(cosine(vector, entry.hook_vector), 4), entry.video_id, entry.hooks[0] if entry.hooks else "")
                    for entry in window
                    if entry.hook_vector is not None and entry.hook_model == model
                ),
                default=(0.0, None, ""),
                key=lambda x: x[0],
            )
            if semantic[0] > cos_limit:
                out.append(
                    RepetitionFinding(
                        "hook",
                        "reject",
                        hook,
                        semantic[0],
                        semantic[1],
                        f"means the same as a recent hook (embedding similarity): {semantic[2]!r}",
                    )
                )
        return out

    def phrases(
        self, script: str, history: Sequence[UsageEntry], signature_phrases: dict[str, int] | None = None
    ) -> list[RepetitionFinding]:
        cfg = self.config.phrases
        n = cfg.ngram or 4
        limit = cfg.max_overlap or 0.3
        text = normalize(script)
        exempt: set[str] = set()
        for phrase, max_per_video in (signature_phrases or {}).items():
            if text.count(normalize(phrase)) <= max_per_video:
                exempt |= set(ngram_fingerprints(phrase, n))
        mine = set(ngram_fingerprints(script, n)) - exempt
        out: list[RepetitionFinding] = []
        if not mine:
            return out
        for entry in history[: cfg.window_videos]:
            overlap = len(mine & entry.phrase_fingerprints) / len(mine)
            if overlap > limit:
                shared = sorted(mine & entry.phrase_fingerprints)[:3]
                out.append(
                    RepetitionFinding(
                        "phrase", "flag", "script", round(overlap, 4), entry.video_id, f"shared phrases: {shared}"
                    )
                )
        return out

    def arc(
        self, arc: Sequence[str], history: Sequence[UsageEntry], *, signature_format: bool = False
    ) -> list[RepetitionFinding]:
        if signature_format or not arc:
            return []
        cfg = self.config.emotional_arcs
        limit = cfg.max_similarity or 0.9
        out = []
        for entry in history[: cfg.window_videos]:
            score = _arc_similarity(arc, entry.arc)
            if score > limit:
                out.append(
                    RepetitionFinding("arc", "flag", " → ".join(arc), score, entry.video_id, "same emotional arc")
                )
        return out

    def visual(
        self, visual: dict[str, Any], history: Sequence[UsageEntry], *, pinned: bool = False
    ) -> list[RepetitionFinding]:
        if pinned or not history:
            return []
        keys = ("shot_sequence", "camera_position", "time_of_day", "wardrobe")
        out = []
        for entry in history[: self.config.visual_patterns.window_videos][:1]:  # consecutive videos
            if all(visual.get(k) is not None and visual.get(k) == entry.visual.get(k) for k in keys):
                out.append(
                    RepetitionFinding(
                        "visual",
                        "flag",
                        f"{visual.get('camera_position')} / {visual.get('time_of_day')}",
                        1.0,
                        entry.video_id,
                        "same shots, camera position, time of day and wardrobe as the previous video",
                    )
                )
        return out

    @staticmethod
    def word_count(text: str) -> int:
        return len(words(text))
