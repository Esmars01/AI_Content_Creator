"""Creator Memory suites [4] (§37): retrieval (filters, budgets, pinned first, forgotten excluded,
version scope, recency decay, conflicts), the repetition guard (hooks, phrases with signature
exemptions, arcs, visual patterns) and the contradiction checker (script vs canon and memory,
stances, DNA vs memory)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ce_config.loader import load_config
from ce_config.schemas import MemoryConfig
from ce_core.identity.creator import CanonFact
from ce_memory import (
    MemoryRecord,
    MemoryRetriever,
    PersonaAssertion,
    RepetitionGuard,
    RetrievalContext,
    UsageEntry,
    dna_conflicts,
    script_contradictions,
    similarity,
    usage_payload,
)
from ce_testing.fixtures import alex_creator_dna, example_spec

pytestmark = pytest.mark.behavior

NOW = datetime(2026, 10, 4, tzinfo=UTC)
_MEMORY = load_config(__import__("pathlib").Path(__file__).resolve().parents[4] / "config", "test").memory
assert _MEMORY is not None
CONFIG: MemoryConfig = _MEMORY
V1, V2, V3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NUMBERS = {V1: 1, V2: 2, V3: 3}


def rec(kind: str, value: dict[str, Any], **kw: Any) -> MemoryRecord:
    category = kind.split(".")[0]
    fields: dict[str, Any] = {
        "id": uuid.uuid4(),
        "category": category,
        "kind": kind,
        "key": kw.pop("key", ""),
        "value": value,
        "text": kw.pop("text", ""),
        "confidence": kw.pop("confidence", 0.8),
        "last_seen_at": kw.pop("last_seen_at", NOW - timedelta(days=10)),
        "value_hash": kw.pop("value_hash", str(sorted(value.items()))),
    }
    fields.update(kw)
    return MemoryRecord(**fields)


def ctx(brief: str = "", version: uuid.UUID = V2) -> RetrievalContext:
    return RetrievalContext(creator_version_id=version, now=NOW, brief=brief, version_numbers=NUMBERS)


RETRIEVER = MemoryRetriever(CONFIG.retrieval)


# ---------------------------------------------------------------------- retrieval


def test_only_active_in_scope_items_enter() -> None:
    active = rec("gaze_habit.thinking_glance", {"direction": "down_left", "typical_ms": 600})
    forgotten = rec("gaze_habit.eye_contact_ratio", {"ratio": 0.7}, status="forgotten")
    proposed = rec("gaze_habit.look_away_frequency", {"frequency": "often"}, status="proposed")
    deleted = rec("social_behavior.warmth", {"level": 0.8}, deleted=True)
    old_scope = rec("social_behavior.sarcasm", {"level": 0.2}, creator_version_to=V1)
    new_scope = rec("social_behavior.directness", {"level": 0.9}, creator_version_from=V3)
    in_scope = rec("social_behavior.seriousness", {"level": 0.4}, creator_version_from=V1, creator_version_to=V2)
    draft = RETRIEVER.retrieve([active, forgotten, proposed, deleted, old_scope, new_scope, in_scope], ctx())
    assert set(draft.item_ids) == {active.id, in_scope.id}


def test_budgets_pinned_first_and_ranking() -> None:
    budget = CONFIG.retrieval.budgets["gaze_habit"]
    items = [
        rec("gaze_habit.thinking_glance", {"n": i}, key=str(i), confidence=0.5 + i / 100) for i in range(budget + 3)
    ]
    pinned_low = rec("gaze_habit.eye_contact_ratio", {"ratio": 0.1}, confidence=0.1, pinned=True)
    draft = RETRIEVER.retrieve([*items, pinned_low], ctx())
    chosen = [i for i in draft.items if i.kind.startswith("gaze_habit")]
    assert len(chosen) == budget and chosen[0].item_id == pinned_low.id  # pinned outranks and always enters
    ranked = [i.confidence for i in chosen[1:]]
    assert ranked == sorted(ranked, reverse=True)
    many_pinned = [rec("gaze_habit.eye_contact_ratio", {"r": i}, key=str(i), pinned=True) for i in range(budget + 2)]
    assert len(RETRIEVER.retrieve(many_pinned, ctx()).items) == budget + 2  # pinned items are never cut


def test_recency_decay_and_keyword_bonus() -> None:
    fresh = rec("speech_habit.recurring_phrase", {"text": "here's the thing"}, key="a", last_seen_at=NOW)
    stale = rec("speech_habit.recurring_phrase", {"text": "real talk"}, key="b", last_seen_at=NOW - timedelta(days=720))
    budget_one = CONFIG.retrieval.model_copy(update={"budgets": {**CONFIG.retrieval.budgets, "speech_habit": 1}})
    assert MemoryRetriever(budget_one).retrieve([fresh, stale], ctx()).item_ids == [fresh.id]
    topical = rec(
        "speech_habit.recurring_phrase",
        {"text": "agents are not chatbots"},
        key="c",
        last_seen_at=NOW - timedelta(days=300),
    )
    draft = MemoryRetriever(budget_one).retrieve([fresh, topical], ctx("why AI agents are misunderstood chatbots"))
    assert draft.item_ids == [topical.id]  # the brief's words outweigh a year of recency


def test_conflicts_resolve_by_precedence_and_are_listed() -> None:
    a = rec("persona_fact", {"subject": "I", "predicate": "live in", "object": "Austin"}, key="i|live in",
            confidence=0.9, source_type="plan", conflict_state="unresolved")  # fmt: skip
    b = rec("persona_fact", {"subject": "I", "predicate": "live in", "object": "Berlin"}, key="i|live in",
            confidence=0.6, source_type="authored", conflict_state="unresolved")  # fmt: skip
    draft = RETRIEVER.retrieve([a, b], ctx())
    assert draft.item_ids == [b.id]  # authored beats a higher-confidence plan item
    (conflict,) = draft.conflicts
    assert conflict["winner"] == str(b.id) and conflict["others"] == [str(a.id)] and conflict["state"] == "unresolved"
    pinned = replace(a, pinned=True)
    assert RETRIEVER.retrieve([pinned, b], ctx()).item_ids == [pinned.id]  # pinned beats authored


def test_drafts_are_deterministic_and_digest_their_content() -> None:
    items = [rec("gaze_habit.thinking_glance", {"n": i}, key=str(i)) for i in range(3)]
    one, two = RETRIEVER.retrieve(items, ctx()), RETRIEVER.retrieve(list(reversed(items)), ctx())
    assert one.digest() == two.digest() and one.content()["items"] == two.content()["items"]


# ---------------------------------------------------------------------- repetition guard

GUARD = RepetitionGuard(CONFIG.repetition)


def _history(**kw: Any) -> list[UsageEntry]:
    return [UsageEntry(video_id=uuid.uuid4(), **kw)]


def test_hooks_too_close_to_recent_hooks_are_rejected() -> None:
    history = _history(hooks=("Everyone thinks AI agents are just smarter chatbots.",))
    findings = GUARD.hooks(
        ["Everyone thinks AI agents are just smarter chatbots!", "What if your inbox could answer itself?"], history
    )
    assert [f.subject for f in findings] == ["Everyone thinks AI agents are just smarter chatbots!"]
    assert findings[0].severity == "reject" and findings[0].score > 0.82
    assert similarity("Most people get AI agents wrong", "Most people misunderstand AI agents") < 0.82


def test_phrase_overlap_is_flagged_but_signature_phrases_are_exempt() -> None:
    script = "here's the thing nobody tells you about cold email it is not dead at all"
    from ce_memory.text import ngram_fingerprints

    history = _history(phrase_fingerprints=frozenset(ngram_fingerprints(script, 4)))
    assert GUARD.phrases(script, history)[0].check == "phrase"
    fresh = "a completely different script about onboarding emails that actually get replies"
    assert GUARD.phrases(fresh, history) == []
    signature = "here's the thing"
    history2 = _history(phrase_fingerprints=frozenset(ngram_fingerprints(signature + " folks", 4)))
    assert GUARD.phrases(f"{signature} folks", history2, {signature: 1}) != []  # the extra word is not exempt
    assert (
        GUARD.phrases(
            f"{signature} again",
            _history(phrase_fingerprints=frozenset(ngram_fingerprints(signature + " again", 3))),
            {signature: 1},
        )
        == []
    )


def test_emotional_arcs_and_visual_patterns() -> None:
    arc = ("confident", "serious", "amused")
    assert GUARD.arc(arc, _history(arc=arc))[0].check == "arc"
    assert GUARD.arc(arc, _history(arc=arc), signature_format=True) == []
    assert GUARD.arc(arc, _history(arc=("excited", "skeptical", "serious"))) == []
    visual = {"shot_sequence": ["talking_head:medium_close_up"], "camera_position": "cam_desk_front",
              "time_of_day": "late_afternoon", "wardrobe": "w1"}  # fmt: skip
    assert GUARD.visual(visual, _history(visual=visual))[0].check == "visual"
    assert GUARD.visual(visual, _history(visual={**visual, "time_of_day": "night"})) == []  # same world, other look
    assert GUARD.visual(visual, _history(visual=visual), pinned=True) == []


# ---------------------------------------------------------------------- contradictions


def test_script_contradicting_the_canon_blocks() -> None:
    canon = [CanonFact(key="home_city", subject="Alex", predicate="lives in", object="Austin")]
    found = script_contradictions(
        [PersonaAssertion("I", "live in", "Berlin", "seg_1")], canon=canon, memory=[], self_names=["Alex"]
    )
    (c,) = found
    assert c.severity == "blocking" and c.against == "canon" and c.ref == "home_city"
    assert (
        script_contradictions([PersonaAssertion("I", "live in", "austin")], canon=canon, memory=[], self_names=["Alex"])
        == []
    )  # same object, other case


def test_memory_contradictions_warn_unless_pinned_and_stances() -> None:
    fact = rec("persona_fact", {"subject": "I", "predicate": "work at", "object": "a bank"}, key="i|work at")
    stance = rec("stance", {"topic": "cold email", "position": "it still works", "strength": 0.8}, key="cold email")
    assertions = [
        PersonaAssertion("I", "works at", "a startup"),
        PersonaAssertion("cold email outreach", "", "it is dead", kind="stance"),
    ]
    found = script_contradictions(assertions, canon=[], memory=[fact, stance])
    assert [(c.against, c.severity) for c in found] == [("memory", "warning"), ("memory", "warning")]
    pinned = script_contradictions(assertions[:1], canon=[], memory=[replace(fact, pinned=True)])
    assert pinned[0].severity == "blocking"
    forgotten = script_contradictions(assertions[:1], canon=[], memory=[replace(fact, status="forgotten")])
    assert forgotten == []


def test_dna_versus_memory_flags_items_for_review() -> None:
    dna = alex_creator_dna()
    canon = dna.identity.canon[0]
    contradicting = rec("persona_fact", {"subject": canon.subject, "predicate": canon.predicate, "object": "Mars"})
    outside = rec("emotional_tendency.emotional_range", {"label": "serious", "min": 0.85, "max": 0.95}, key="serious")
    fine = rec("emotional_tendency.emotional_range", {"label": "serious", "min": 0.3, "max": 0.7}, key="serious2")
    flagged = dict(dna_conflicts(dna, [contradicting, outside, fine]))
    assert set(flagged) == {contradicting.id, outside.id}


# ---------------------------------------------------------------------- usage payload


def test_usage_payload_of_the_example() -> None:
    payload = usage_payload(example_spec(), "char_alex", behavior_signatures=[{"arc_shape": [0.1]}])
    assert payload["arc_signature"]["labels"] == ["confident", "serious"]
    assert payload["visual_signature"]["camera_position"] == "cam_desk_front"
    assert payload["visual_signature"]["shot_sequence"] == ["talking_head:medium_close_up"]
    assert payload["phrase_fingerprints"] and payload["behavior_signatures"] == [{"arc_shape": [0.1]}]
    assert len(payload["world_version_ids"]) == 1
