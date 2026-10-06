"""The contradiction checker (§18.7).

- **Script vs persona**: persona assertions and stances extracted from the script (Director stage
  6) are compared with the Creator DNA canon and the snapshot's active `persona_fact`/`stance`
  items. A different object for the same (subject, predicate) is a contradiction: blocking when
  the canon fact or memory item is pinned (approval needs a logged override or a script edit),
  a warning otherwise.
- **Memory vs memory** happens on insert (the API marks both items `unresolved`, §18.7); retrieval
  lists unresolved conflicts in the snapshot.
- **DNA vs memory**: a new creator version whose canon or emotion ranges contradict active items
  flags them for review; nothing is deleted.

Subjects that refer to the creator ("I", "me", the display name) normalize to `self`; predicates
are compared case- and inflection-insensitively ("live in" = "lives in").
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from ce_core.identity.creator import CanonFact, CreatorDNA

from ce_memory.records import MemoryRecord
from ce_memory.text import keywords, normalize, words

__all__ = ["Contradiction", "PersonaAssertion", "dna_conflicts", "script_contradictions"]

SELF_WORDS = frozenset({"i", "me", "my", "myself", "mine", "i'm", "im", "self", "creator", "the creator"})


@dataclass(frozen=True)
class PersonaAssertion:
    subject: str
    predicate: str
    object: str
    segment_key: str | None = None
    kind: Literal["fact", "stance"] = "fact"


@dataclass(frozen=True)
class Contradiction:
    assertion: PersonaAssertion
    against: Literal["canon", "memory"]
    ref: str
    existing: str
    severity: Literal["blocking", "warning"]

    def as_dict(self) -> dict[str, Any]:
        a = self.assertion
        return {
            "assertion": {"subject": a.subject, "predicate": a.predicate, "object": a.object, "kind": a.kind},
            "segment_key": a.segment_key,
            "against": self.against,
            "ref": self.ref,
            "existing": self.existing,
            "severity": self.severity,
        }


def _subject(text: str, self_names: Iterable[str]) -> str:
    norm = " ".join(words(text))
    names = {" ".join(words(n)) for n in self_names}
    return "self" if norm in SELF_WORDS or norm in names else norm


def _predicate(text: str) -> str:
    parts = words(text)
    if parts and len(parts[0]) > 3 and parts[0].endswith("s") and not parts[0].endswith("ss"):
        parts[0] = parts[0][:-1]
    if parts and parts[0] in {"am", "is", "are"}:
        parts[0] = "be"
    return " ".join(parts)


def _object(text: str) -> str:
    return " ".join(words(text))


def _severity(pinned: bool) -> Literal["blocking", "warning"]:
    return "blocking" if pinned else "warning"


def _same_topic(a: str, b: str) -> bool:
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return normalize(a).strip() == normalize(b).strip()
    return len(ka & kb) / min(len(ka), len(kb)) >= 0.5


def script_contradictions(
    assertions: Sequence[PersonaAssertion],
    *,
    canon: Sequence[CanonFact],
    memory: Sequence[MemoryRecord],
    self_names: Iterable[str] = (),
) -> list[Contradiction]:
    names = list(self_names)
    out: list[Contradiction] = []
    for assertion in assertions:
        if assertion.kind == "stance":
            for item in memory:
                if item.kind != "stance" or item.status != "active":
                    continue
                topic, position = str(item.value.get("topic", "")), str(item.value.get("position", ""))
                if _same_topic(assertion.subject, topic) and _object(assertion.object) != _object(position):
                    existing = f"{topic}: {position}"
                    out.append(Contradiction(assertion, "memory", str(item.id), existing, _severity(item.pinned)))
            continue
        subject = _subject(assertion.subject, names)
        predicate = _predicate(assertion.predicate)
        obj = _object(assertion.object)
        for fact in canon:
            same_slot = (_subject(fact.subject, names), _predicate(fact.predicate)) == (subject, predicate)
            if same_slot and _object(fact.object) != obj:
                existing = f"{fact.subject} {fact.predicate} {fact.object}"
                out.append(Contradiction(assertion, "canon", fact.key, existing, _severity(fact.pinned)))
        for item in memory:
            if item.kind != "persona_fact" or item.status != "active":
                continue
            v = item.value
            slot = (_subject(str(v.get("subject", "")), names), _predicate(str(v.get("predicate", ""))))
            if slot == (subject, predicate) and _object(str(v.get("object", ""))) != obj:
                existing = f"{v.get('subject')} {v.get('predicate')} {v.get('object')}"
                out.append(Contradiction(assertion, "memory", str(item.id), existing, _severity(item.pinned)))
    return out


def dna_conflicts(dna: CreatorDNA, items: Sequence[MemoryRecord]) -> list[tuple[UUID, str]]:
    """Active items a new creator version's DNA contradicts (flagged for review, never deleted)."""
    names = [dna.identity.display_name]
    flagged: list[tuple[UUID, str]] = []
    for item in items:
        if item.status != "active":
            continue
        if item.kind == "persona_fact":
            assertion = PersonaAssertion(
                str(item.value.get("subject", "")),
                str(item.value.get("predicate", "")),
                str(item.value.get("object", "")),
            )
            for c in script_contradictions([assertion], canon=dna.identity.canon, memory=[], self_names=names):
                flagged.append((item.id, f"contradicts canon {c.ref}: {c.existing}"))
        elif item.kind == "emotional_tendency.emotional_range":
            label = str(item.value.get("label", ""))
            bounds = dna.behavior.emotion_ranges.get(label)
            low, high = float(item.value.get("min", 0.0)), float(item.value.get("max", 1.0))
            if bounds is not None and (high < bounds[0] or low > bounds[1]):
                flagged.append((item.id, f"{label} range [{low}, {high}] lies outside the DNA range {list(bounds)}"))
    return flagged
