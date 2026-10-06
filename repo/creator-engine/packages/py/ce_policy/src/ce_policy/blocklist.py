"""Blocklists (§32): names, terms and topics that planning may not produce.

The Director's output — script segments, scene and shot prompts, titles — is checked as text at
stage 11. A hit is a blocking `policy` finding that cannot be overridden from the plan review
(the list is an operator rule). Matching is case-insensitive on whole words, with no Unicode
normalization beyond case folding, so "Ünal" does not match "Unal". Likeness checks against
`protected_persons` embeddings are not compared yet (no likeness check is implemented).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import regex
from ce_core.behavior.plan_report import Finding

__all__ = ["BlocklistChecker", "BlocklistHit"]

_LISTS = (("protected_persons", "a protected person"), ("terms", "a blocked term"), ("topics", "a blocked topic"))


@dataclass(frozen=True)
class BlocklistHit:
    list_name: str
    entry: str
    ref: str  # a SpecPath
    start: int
    end: int


def _pattern(entry: str) -> regex.Pattern[str]:
    words = [regex.escape(w) for w in entry.split()]
    return regex.compile(r"(?<![\p{L}\p{N}])" + r"\s+".join(words) + r"(?![\p{L}\p{N}])", regex.IGNORECASE)


class BlocklistChecker:
    def __init__(self, *, protected_persons: Iterable[str] = (), terms: Iterable[str] = (), topics: Iterable[str] = ()):
        lists = {"protected_persons": protected_persons, "terms": terms, "topics": topics}
        self._patterns = [
            (name, entry, _pattern(entry)) for name, entries in lists.items() for entry in entries if entry.strip()
        ]

    @classmethod
    def from_config(cls, config: object | None) -> BlocklistChecker:
        if config is None:
            return cls()
        return cls(
            protected_persons=getattr(config, "protected_persons", ()),
            terms=getattr(config, "terms", ()),
            topics=getattr(config, "topics", ()),
        )

    def hits(self, texts: Iterable[tuple[str, str]]) -> list[BlocklistHit]:
        """`texts` are (SpecPath, text) pairs."""
        found: list[BlocklistHit] = []
        for ref, text in texts:
            for name, entry, pattern in self._patterns:
                found.extend(BlocklistHit(name, entry, ref, m.start(), m.end()) for m in pattern.finditer(text))
        return found

    def findings(self, texts: Iterable[tuple[str, str]]) -> list[Finding]:
        labels = dict(_LISTS)
        return [
            Finding(
                kind="policy",
                severity="blocking",
                message=f"{labels[hit.list_name].capitalize()} appears in the plan: {hit.entry!r}",
                refs=[hit.ref],
                detail={
                    "id": f"blocklist:{hit.list_name}:{hit.ref}:{hit.start}",
                    "check": "blocklist",
                    "list": hit.list_name,
                    "entry": hit.entry,
                    "span": [hit.start, hit.end],
                    "overridable": False,
                },
            )
            for hit in self.hits(texts)
        ]
