"""The memory loop's write-path decisions (§18.4, Phase 12) and embedding use in retrieval and the
repetition guard (§18.5, §18.6): pure functions, no database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ce_config.schemas import RepetitionConfig, RepetitionWindow, RetrievalConfig
from ce_memory import MemoryRecord, MemoryRetriever, RepetitionGuard, RetrievalContext, UsageEntry
from ce_memory.loop import (
    EditSignature,
    HabitEvidence,
    HabitMerge,
    activation_targets,
    edit_signatures,
    habit_evidence,
    merge_habit,
    persona_proposals,
    preference_proposals,
)
from ce_memory.retrieval import RETRIEVER, RETRIEVER_EMBEDDING

pytestmark = [pytest.mark.behavior]

NOW = datetime(2026, 10, 5, tzinfo=UTC)


def test_persona_proposals_normalize_the_creator_and_dedupe() -> None:
    out = persona_proposals(
        [
            {"segment_key": "seg_1", "subject": "I", "predicate": "live in", "object": "Austin", "kind": "fact"},
            {"segment_key": "seg_2", "subject": "me", "predicate": "live in", "object": "Austin", "kind": "fact"},
            {
                "segment_key": "seg_3",
                "subject": "Alex",
                "predicate": "remote work",
                "object": "overrated",
                "kind": "stance",
            },
            {"segment_key": "seg_4", "subject": "", "predicate": "is", "object": "x", "kind": "fact"},
        ],
        self_names=["Alex"],
    )
    assert [p.kind for p in out] == ["persona_fact", "stance"]
    assert out[0].value == {"subject": "Alex", "predicate": "live in", "object": "Austin"}
    assert out[1].value["topic"] == "remote work" and out[1].value["position"] == "overrated"


def test_activation_targets_only_this_videos_plan_items() -> None:
    video, other = str(uuid.uuid4()), str(uuid.uuid4())
    items = [
        {"id": "a", "kind": "persona_fact", "status": "proposed", "source": {"type": "plan", "video_id": video}},
        {"id": "b", "kind": "persona_fact", "status": "proposed", "source": {"type": "plan", "video_id": other}},
        {"id": "c", "kind": "stance", "status": "active", "source": {"type": "plan", "video_id": video}},
        {"id": "d", "kind": "preference", "status": "proposed", "source": {"type": "plan", "video_id": video}},
        {"id": "e", "kind": "stance", "status": "proposed", "source": {"type": "user_edit", "video_id": video}},
    ]
    assert activation_targets(items, version_id="v", video_id=video) == ["a"]


def test_habit_evidence_scales_and_clamps() -> None:
    features: dict[str, dict[str, Any]] = {
        "gaze_to_camera_ratio": {"value": 0.8, "n": 40, "mock": True},
        "head_motion_energy": {"value": 25.0, "n": 3},
        "gesture_energy": {"value": None},
    }
    mapping = [
        {"feature": "gaze_to_camera_ratio", "kind": "gaze_habit.eye_contact_ratio", "field": "ratio", "scale": 1.0},
        {"feature": "head_motion_energy", "kind": "gesture_habit.head_movement", "field": "amplitude", "scale": 10.0},
        {"feature": "gesture_energy", "kind": "gesture_habit.gesture_density", "field": "density", "scale": 1.0},
    ]
    out = habit_evidence(features, mapping)
    assert [(e.kind, e.value, e.mock) for e in out] == [
        ("gaze_habit.eye_contact_ratio", 0.8, True),
        ("gesture_habit.head_movement", 1.0, False),
    ]


def _merge(previous: list[dict[str, object]], value: float, video: str, **kw: object) -> HabitMerge:
    args = {
        "min_evidence_count": 3,
        "min_confidence": 0.7,
        "quantum": 0.05,
        "conflict_tolerance": 0.15,
        "allow_mock": False,
        "authored_value": None,
    } | kw
    return merge_habit(
        HabitEvidence("gaze_habit.eye_contact_ratio", "ratio", value, 10, bool(kw.get("mock", False)), "f"),
        video_id=video,
        previous=previous,
        **{k: v for k, v in args.items() if k != "mock"},  # type: ignore[arg-type]
    )


def test_habits_merge_per_video_and_promote_past_the_thresholds() -> None:
    first = _merge([], 0.80, "v1")
    assert first.evidence_count == 1 and not first.promote and first.value == {"ratio": 0.8}
    again = _merge(first.evidence, 0.70, "v1")  # the same video re-observed: replaces, never double-counts
    assert again.evidence_count == 1 and again.value == {"ratio": 0.7}
    second = _merge(again.evidence, 0.72, "v2")
    third = _merge(second.evidence, 0.74, "v3")
    assert third.evidence_count == 3 and third.promote, third.reasons
    assert third.value == {"ratio": 0.7}  # mean 0.72 quantized to 0.05
    assert 0.7 <= third.confidence < 1.0


def test_spread_mock_evidence_and_authored_conflicts_block_promotion() -> None:
    spread = _merge(_merge(_merge([], 0.1, "a").evidence, 0.9, "b").evidence, 0.5, "c")
    assert not spread.promote and "confidence" in spread.reasons[0]
    mock = _merge(_merge(_merge([], 0.8, "a", mock=True).evidence, 0.8, "b").evidence, 0.8, "c")
    assert not mock.promote and "mock" in mock.reasons[0]
    allowed = _merge(_merge(_merge([], 0.8, "a", mock=True).evidence, 0.8, "b").evidence, 0.8, "c", allow_mock=True)
    assert allowed.promote
    conflict = _merge(allowed.evidence, 0.8, "d", authored_value=0.4)
    assert conflict.conflicts_with_authored and not conflict.promote
    close = _merge(allowed.evidence, 0.8, "d", authored_value=0.7)
    assert not close.conflicts_with_authored


def test_edit_signatures_and_repeated_edit_preferences() -> None:
    dims = {"small_smile": "facial_expression", "look_away": "gaze"}
    smile_less = [{"op": "set_behavior_event", "event_type": "small_smile", "changes": {"intensity_delta": -0.2}}]
    sigs = edit_signatures(smile_less, event_dimensions=dims)
    assert sigs == [EditSignature("facial_expression", "small_smile", -0.2)]
    calmer = [
        {"op": "set_acting", "changes": {"emotion": {"displayed": {"label": "excited", "intensity_delta": -0.3}}}}
    ]
    assert edit_signatures(calmer, event_dimensions=dims) == [EditSignature("emotion_visual", "excited", -0.3)]
    assert edit_signatures([{"op": "set_camera", "framing": "close_up"}], event_dimensions=dims) == []
    history = [
        ("p3", sigs),
        ("p2", sigs),
        ("p1", sigs),
        ("p0", [EditSignature("facial_expression", "small_smile", 0.1)]),
    ]
    assert preference_proposals(history[:2], threshold=3) == []
    (pref,) = preference_proposals(history, threshold=3)
    assert pref.value == {
        "dimension": "facial_expression",
        "label": "small_smile",
        "delta": -0.2,
        "note": "from 3 edits",
    }
    assert pref.proposal_ids == ("p3", "p2", "p1") and pref.support == 3


def _record(text: str, vector: tuple[float, ...] | None, model: str | None, kind: str = "persona_fact") -> MemoryRecord:
    return MemoryRecord(
        id=uuid.uuid4(),
        category=kind.split(".")[0],
        kind=kind,
        key=text,
        value={"subject": "Alex", "predicate": "says", "object": text},
        text=text,
        confidence=0.5,
        last_seen_at=NOW - timedelta(days=1),
        embedding=vector,
        embedding_model=model,
    )


def test_retrieval_ranks_by_embedding_of_the_same_model_only() -> None:
    config = RetrievalConfig(
        budgets={"persona_fact": 1},
        recency_half_life_days={"persona_fact": 365},
        confidence_weight=0.5,
        conflict_precedence=["pinned", "authored", "confidence", "recency"],
    )
    near = _record("dogs", (1.0, 0.0), "m1")
    far = _record("cats", (0.0, 1.0), "m1")
    other_model = _record("birds", (1.0, 0.0), "m2")
    version = uuid.uuid4()
    ctx = RetrievalContext(
        creator_version_id=version, now=NOW, brief="an unrelated brief", brief_vector=(1.0, 0.0), embedding_model="m1"
    )
    draft = MemoryRetriever(config).retrieve([far, other_model, near], ctx)
    assert [i.item_id for i in draft.items] == [near.id]
    assert draft.params["retriever"] == RETRIEVER_EMBEDDING and draft.params["embedding_model"] == "m1"
    keyword = MemoryRetriever(config).retrieve(
        [far, near], RetrievalContext(creator_version_id=version, now=NOW, brief="cats")
    )
    assert [i.item_id for i in keyword.items] == [far.id] and keyword.params["retriever"] == RETRIEVER


def test_hook_guard_rejects_semantic_repeats_of_the_same_model() -> None:
    window = RepetitionWindow(window_videos=5, max_similarity=0.82)
    guard = RepetitionGuard(
        RepetitionConfig(
            hooks=window,
            phrases=RepetitionWindow(window_videos=5),
            emotional_arcs=window,
            visual_patterns=window,
            behavior_signatures=window,
        )
    )
    history = [
        UsageEntry(
            video_id=uuid.uuid4(), hooks=("Everyone gets agents wrong.",), hook_vector=(1.0, 0.0), hook_model="m"
        )
    ]
    hooks = ["Nobody understands how AI agents work.", "Here is a fresh angle on cooking."]
    found = guard.hooks(hooks, history, vectors=[(0.99, 0.05), (0.0, 1.0)], model="m", max_cosine=0.9)
    assert [f.subject for f in found] == [hooks[0]] and "embedding" in found[0].detail
    assert guard.hooks(hooks, history, vectors=[(0.99, 0.05), (0.0, 1.0)], model="other", max_cosine=0.9) == []
    assert guard.hooks(hooks, history) == []  # lexically different: keyword guard alone lets them pass
