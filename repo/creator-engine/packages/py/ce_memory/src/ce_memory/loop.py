"""The memory loop's decisions (§18.4, Phase 12), as pure functions over plain data.

`MemoryUpdateWorkflow` (`ce_exec.memory_job`) loads rows, calls these, and writes the result. Nothing
here touches the database, a model or the clock, so every write path is unit-testable.

Write paths (§18.4):

| trigger | function | writes |
| --- | --- | --- |
| version approved (previz) | `persona_proposals` | the script's persona facts and stances, `proposed` |
| version `ready` | `habit_evidence` + `merge_habit` | measured habits merged per (kind, key); promoted |
| version exported | `activation_targets` | the version's proposed persona facts and stances become `active` |
| repeated user edits | `edit_signatures` + `preference_proposals` | a `proposed` preference after N edits of one kind |
| accepted critique proposal | `edit_signatures` + `preference_proposals` (threshold 1) | a `proposed` preference |
| `memory_feedback` | (ce_exec.editing, Phase 6) | a `proposed` avoidance |

Measured habits are continuous: an observation item's value is the running mean of its evidence,
quantized (`quantum`), so a new video moves it instead of opening a conflict. It conflicts with a
live authored item only when the measured mean leaves the authored value by more than
`conflict_tolerance` (§18.7: both rows coexist; the user resolves it).
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "EditSignature",
    "HabitEvidence",
    "HabitMerge",
    "PersonaProposal",
    "PreferenceProposal",
    "activation_targets",
    "edit_signatures",
    "habit_evidence",
    "merge_habit",
    "persona_proposals",
    "preference_proposals",
]


# ---------------------------------------------------------------------- persona facts and stances
@dataclass(frozen=True)
class PersonaProposal:
    kind: str  # persona_fact | stance
    value: dict[str, Any]
    text: str
    segment_key: str | None


def persona_proposals(
    assertions: Iterable[Mapping[str, Any]], *, self_names: Iterable[str] = ()
) -> list[PersonaProposal]:
    """The script's persona assertions (fact-check stage output) as memory proposals. Subjects that
    mean the creator normalize to the creator's name (the first of `self_names`) so dedup keys match
    authored items. Stances need a topic and a position; facts need subject, predicate and object."""
    names = [n for n in self_names if n]
    me = names[0] if names else "self"
    self_words = {"i", "me", "myself", "self", *(n.casefold() for n in names)}
    out: list[PersonaProposal] = []
    seen: set[tuple[str, str]] = set()
    for a in assertions:
        kind = str(a.get("kind") or "fact")
        subject = str(a.get("subject") or "").strip()
        predicate = str(a.get("predicate") or "").strip()
        obj = str(a.get("object") or "").strip()
        if subject.casefold() in self_words:
            subject = me
        if kind == "stance":
            topic, position = predicate or subject, obj
            if not topic or not position:
                continue
            value: dict[str, Any] = {"topic": topic, "position": position, "strength": 0.5}
            text = f"{me}'s stance on {topic}: {position}"
            proposal = PersonaProposal("stance", value, text, a.get("segment_key"))
        else:
            if not (subject and predicate and obj):
                continue
            value = {"subject": subject, "predicate": predicate, "object": obj}
            text = f"{subject} {predicate} {obj}"
            proposal = PersonaProposal("persona_fact", value, text, a.get("segment_key"))
        ident = (proposal.kind, repr(sorted(proposal.value.items())))
        if ident not in seen:
            seen.add(ident)
            out.append(proposal)
    return out


def activation_targets(items: Iterable[Mapping[str, Any]], *, version_id: str, video_id: str) -> list[str]:
    """Ids of proposed persona facts and stances planned for this export's video (§18.4: the creator
    "said it publicly"). Items from other videos stay proposed."""
    out = []
    for item in items:
        source = dict(item.get("source") or {})
        if item.get("status") != "proposed" or item.get("kind") not in ("persona_fact", "stance"):
            continue
        if source.get("type") != "plan":
            continue
        if str(source.get("video_id")) == video_id or str(source.get("version_id")) == version_id:
            out.append(str(item["id"]))
    return out


# ---------------------------------------------------------------------- habits from observation
@dataclass(frozen=True)
class HabitEvidence:
    kind: str
    field: str
    value: float
    n: int
    mock: bool
    feature: str


def habit_evidence(
    features: Mapping[str, Mapping[str, Any]], mapping: Sequence[Mapping[str, Any]]
) -> list[HabitEvidence]:
    """Measured features of one video (ce_qc.consistency.video_features shape) → habit evidence,
    following `config/memory.yaml` `observation_habits`: `{feature, kind, field, scale}`; the
    measured value is divided by `scale` and clamped to 0..1."""
    out: list[HabitEvidence] = []
    for rule in mapping:
        entry = features.get(str(rule["feature"]))
        if not entry or entry.get("value") is None:
            continue
        scale = float(rule.get("scale", 1.0)) or 1.0
        value = max(0.0, min(1.0, float(entry["value"]) / scale))
        out.append(
            HabitEvidence(
                kind=str(rule["kind"]),
                field=str(rule["field"]),
                value=value,
                n=int(entry.get("n") or 1),
                mock=bool(entry.get("mock")),
                feature=str(rule["feature"]),
            )
        )
    return out


@dataclass
class HabitMerge:
    value: dict[str, Any]
    evidence: list[dict[str, Any]]
    evidence_count: int
    confidence: float
    promote: bool
    conflicts_with_authored: bool
    reasons: list[str] = field(default_factory=list)


def _quantize(value: float, quantum: float) -> float:
    return round(round(value / quantum) * quantum, 6) if quantum > 0 else round(value, 6)


def merge_habit(
    evidence: HabitEvidence,
    *,
    video_id: str,
    previous: Sequence[Mapping[str, Any]],
    authored_value: float | None,
    min_evidence_count: int,
    min_confidence: float,
    quantum: float,
    conflict_tolerance: float,
    allow_mock: bool,
) -> HabitMerge:
    """One video's evidence merged into the observation item of its kind (§18.4).

    `previous` is the item's evidence list (one entry per distinct video). A video already in the
    list replaces its own entry (re-observation after a re-render never double-counts). Confidence
    grows with distinct videos and shrinks with spread: `(1 − 1/(n+1)) · (1 − min(1, 2·stdev))`."""
    entries = [dict(e) for e in previous if str(e.get("video_id")) != video_id]
    entries.append({"video_id": video_id, "value": round(evidence.value, 6), "mock": evidence.mock})
    values = [float(e["value"]) for e in entries]
    n = len(entries)
    mean = statistics.fmean(values)
    spread = statistics.pstdev(values) if n > 1 else 0.0
    confidence = round((1 - 1 / (n + 1)) * (1 - min(1.0, 2 * spread)), 4)
    reasons: list[str] = []
    mock = any(bool(e.get("mock")) for e in entries)
    conflicts = authored_value is not None and abs(mean - authored_value) > conflict_tolerance
    promote = n >= min_evidence_count and confidence >= min_confidence
    if promote and mock and not allow_mock:
        promote = False
        reasons.append("evidence comes from mock engines (promotion.allow_mock_evidence is off)")
    if promote and conflicts:
        promote = False
        reasons.append("the measured habit differs from the creator's authored value; resolve the conflict")
    if not promote and n < min_evidence_count:
        reasons.append(f"{n} of {min_evidence_count} videos of evidence")
    elif not promote and confidence < min_confidence:
        reasons.append(f"confidence {confidence:.2f} below {min_confidence:.2f}")
    return HabitMerge(
        value={evidence.field: _quantize(mean, quantum)},
        evidence=entries,
        evidence_count=n,
        confidence=confidence,
        promote=promote,
        conflicts_with_authored=bool(conflicts),
        reasons=reasons,
    )


# ---------------------------------------------------------------------- preferences from edits
@dataclass(frozen=True)
class EditSignature:
    """What an applied edit changed, in memory terms: a behavior dimension, an optional label and
    a signed delta. Signatures with the same (dimension, label, direction) are the "same kind"."""

    dimension: str
    label: str | None
    delta: float

    @property
    def group(self) -> tuple[str, str | None, int]:
        return (self.dimension, self.label, 1 if self.delta > 0 else -1 if self.delta < 0 else 0)


def edit_signatures(ops: Iterable[Mapping[str, Any]], *, event_dimensions: Mapping[str, str]) -> list[EditSignature]:
    """Signatures of one edit's operations. Covered: displayed-emotion intensity changes
    (`set_acting`), pacing (`set_pacing`), behavior-event intensity (`set_behavior_event`),
    removed events (`remove_behavior_event`, a negative delta on the event's dimension) and
    added events (positive). Other operations carry no habit preference."""
    out: list[EditSignature] = []
    for op in ops:
        name = op.get("op")
        if name == "set_acting":
            changes = dict(op.get("changes") or {})
            emotion = dict(changes.get("emotion") or {})
            displayed = dict(emotion.get("displayed") or {})
            delta = displayed.get("intensity_delta")
            if isinstance(delta, (int, float)) and delta:
                out.append(EditSignature("emotion_visual", displayed.get("label"), float(delta)))
            elif isinstance(displayed.get("intensity"), (int, float)) and displayed.get("label"):
                out.append(EditSignature("emotion_visual", str(displayed["label"]), 0.0))
        elif name == "set_pacing":
            delta = op.get("target_wpm_delta")
            if isinstance(delta, (int, float)) and delta:
                out.append(EditSignature("prosody_rate", None, float(delta)))
        elif name == "set_behavior_event":
            changes = dict(op.get("changes") or {})
            delta = changes.get("intensity_delta")
            dim = event_dimensions.get(str(op.get("event_type") or changes.get("type") or ""))
            if dim and isinstance(delta, (int, float)) and delta:
                out.append(EditSignature(dim, str(op.get("event_type") or changes.get("type")), float(delta)))
        elif name in ("remove_behavior_event", "add_behavior_event"):
            event = dict(op.get("event") or {})
            etype = str(event.get("type") or op.get("event_type") or "")
            dim = event_dimensions.get(etype)
            if dim:
                out.append(EditSignature(dim, etype, -0.1 if name == "remove_behavior_event" else 0.1))
    return [s for s in out if s.delta != 0.0]


@dataclass(frozen=True)
class PreferenceProposal:
    value: dict[str, Any]
    text: str
    support: int
    proposal_ids: tuple[str, ...]


def preference_proposals(
    history: Sequence[tuple[str, Sequence[EditSignature]]], *, threshold: int
) -> list[PreferenceProposal]:
    """`history` = (edit proposal id, its signatures) of one creator's applied edits, newest first.
    A group (dimension, label, direction) seen in at least `threshold` distinct edits becomes a
    proposed preference whose delta is the mean of its deltas."""
    groups: dict[tuple[str, str | None, int], list[tuple[str, EditSignature]]] = defaultdict(list)
    for proposal_id, signatures in history:
        seen_here: set[tuple[str, str | None, int]] = set()
        for sig in signatures:
            if sig.group in seen_here:
                continue
            seen_here.add(sig.group)
            groups[sig.group].append((proposal_id, sig))
    out: list[PreferenceProposal] = []
    for (dimension, label, direction), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
        if len(members) < threshold or direction == 0:
            continue
        delta = round(statistics.fmean(s.delta for _, s in members), 4)
        what = f"{label} " if label else ""
        word = "more" if delta > 0 else "less"
        out.append(
            PreferenceProposal(
                value={"dimension": dimension, "label": label, "delta": delta, "note": f"from {len(members)} edits"},
                text=f"Prefers {word} {what}{dimension.replace('_', ' ')} (repeated edits)".replace("  ", " "),
                support=len(members),
                proposal_ids=tuple(pid for pid, _ in members),
            )
        )
    return out


def count_by(items: Iterable[str]) -> dict[str, int]:
    return dict(Counter(items))
