"""Acceptance fixtures (edits) (§37, Phase 6 DoD): every edit example of section 2 maps to its
expected operation types and dirty sets, in fixture mode. The base is the two-scene example
(`two_scene_spec_dict`); accent changes use the fixture voice version (voice design is Phase 10).

Each test runs the whole proposal path without persistence: reference resolution and the `edit`
stage (fixture LLM) → operations → SpecPatch → validation (schema, references, vocabulary, locks,
policy) → the derived version's impact → the predicted coverage delta and alternatives.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from ce_build import ParentBuild, assemble, records_for
from ce_core.edit.ops import parse_operations
from ce_core.spec.anchors import estimate_segment_timings
from ce_core.spec.videospec import VideoSpec
from ce_director.edit import EditContext, EditDirector, EditPlan, EditRequest, RecordOption, Selection
from ce_director.proposal import Proposal, ProposalInputs, compute_proposal
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.director import director_deps
from ce_testing.edits import first_build
from ce_testing.fixtures import ALEX, example_references, two_scene_spec_dict

pytestmark = [pytest.mark.acceptance, pytest.mark.behavior]

OPTIONS = [
    RecordOption("world", ALEX.WORLD_VERSION_ID, "Alex's home office", "home office", ("warm", "lived-in")),
    RecordOption("world", ALEX.OFFICE_WORLD_VERSION_ID, "Modern office", "office", ("modern", "bright", "minimal")),
    RecordOption("wardrobe", ALEX.WARDROBE_VERSION_ID, "grey hoodie", "a plain mid-grey zip hoodie", ("casual",)),
    RecordOption("wardrobe", ALEX.WARDROBE_NAVY_VERSION_ID, "navy sweater", "a fitted navy crewneck", ("smart",)),
    RecordOption("voice", ALEX.VOICE_VERSION_ID, "Alex voice v1", "relaxed American", accent="general_american"),
    RecordOption("voice", ALEX.VOICE_UK_VERSION_ID, "Alex voice v2", "southern British", accent="southern_british"),
]


def _word_times(spec: VideoSpec) -> dict[str, list[tuple[float, float]]]:
    order = [
        (k, spec.script.segment(k).text) for s in sorted(spec.scenes, key=lambda s: s.order) for k in s.segment_keys
    ]
    timings = estimate_segment_timings(order, 150.0, start_s=0.3)
    return {k: [(w.start_s, w.end_s) for w in t.words] for k, t in timings.items()}


def _parent(data: dict[str, Any]) -> tuple[VideoSpec, ParentBuild]:
    built = first_build(data)
    rows = [
        r for n in built.graph.nodes for r in records_for(n, artifact_id=f"art:{n.key}", effective_seed=n.seed_base)
    ]
    return built.spec, ParentBuild(built.graph, assemble(rows), spec=built.spec, outputs=dict(built.outputs))


def propose(
    instruction: str, *, voice_locked: bool = False, selection: Selection | None = None, allow_template: bool = False
) -> tuple[EditPlan, Proposal]:
    data = two_scene_spec_dict()
    if not voice_locked:
        data["locks"] = []
    spec, parent = _parent(data)
    refs = example_build_refs()
    ctx = EditContext(spec=spec, refs=refs, word_times=_word_times(spec), options=OPTIONS)
    plan = asyncio.run(
        EditDirector(director_deps(allow_template=allow_template)).plan(
            EditRequest(instruction=instruction, selection=selection), ctx
        )
    )
    inputs = ProposalInputs(
        parent_spec=spec,
        parent_refs=refs,
        bundle=config_bundle(),
        catalog=mock_catalog(),
        references=example_references(),
        refs_for=lambda _spec: refs,
        parent_build=parent,
        actor="director",
    )
    return plan, compute_proposal(inputs, plan.operations)


def op_types(plan: EditPlan) -> list[str]:
    return [op.op for op in plan.operations]


def generation(p: Proposal) -> set[str]:
    return set(p.impact["generation"])


def runs(p: Proposal) -> set[str]:
    return {*p.impact["regenerate"], *p.impact["cascade"]}


SCENE_REVEAL = ("tts.segment:seg_2", "avatar.render:sht_4", "image.keyframe:sht_4", "world.plate:scn_reveal")
SCENE_HOOK = ("tts.segment:seg_1", "avatar.render:sht_1", "image.keyframe:sht_1", "world.plate:scn_hook")


def touches(keys: set[str], prefixes: tuple[str, ...]) -> set[str]:
    return {k for k in keys if k.startswith(prefixes)}


def test_make_the_first_3_seconds_more_aggressive() -> None:
    plan, p = propose("make the first 3 seconds more aggressive")
    assert op_types(plan) == ["set_acting", "set_camera"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    hook = p.spec.scene("scn_hook")
    assert hook.acting is not None and len(hook.acting.states) == 2  # split at the 3-second boundary
    first = hook.acting.states[0]
    assert first.emotion is not None and first.emotion.displayed.label == "determined"
    assert first.strategies is not None and first.strategies.prosody == "assertive" and first.source == "user_edit"
    assert hook.shots[0].camera.moves[0].type == "punch_in"
    assert touches(generation(p), ("tts.segment:seg_1", "avatar.render:sht_1"))
    assert not touches(generation(p), SCENE_REVEAL)  # the reveal is untouched


def test_make_him_more_skeptical_with_the_second_scene_selected() -> None:
    plan, p = propose("Make him more skeptical", selection=Selection(scene_keys=["scn_reveal"]))
    assert op_types(plan) == ["set_acting", "add_behavior_event"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    reveal = p.spec.scene("scn_reveal")
    assert reveal.acting is not None
    state = reveal.acting.states[0]
    assert state.emotion is not None and state.emotion.displayed.label == "skeptical"
    assert state.emotion.displayed.intensity == pytest.approx(0.8)  # 0.6 + 0.2
    assert state.strategies is not None and state.strategies.gaze == "side_glance"
    brow = next(e for e in reveal.acting.events if e.type == "eyebrow_raise")
    assert brow.at is not None and (brow.at.segment_key, brow.at.word) == ("seg_2", 5)  # the claim word
    assert touches(generation(p), ("tts.segment:seg_2", "avatar.render:sht_4"))
    assert not touches(generation(p), SCENE_HOOK)
    # §28: on the global engine with voice unlocked, delivery carries it; the eyebrow raise is unsupported
    items = {(i["item_ref"].rsplit("/", 1)[-1], i["dimension"]): i for i in p.coverage_delta["items"]}
    added = next(v for (ref, _), v in items.items() if ref.startswith(f"events[{brow.key}]"))
    assert added["change"] == "added" and added["after"]["level"] == "UNSUPPORTED"
    vocal = items[("emotion", "emotion_vocal")]
    assert vocal["after"]["level"] == "APPROXIMATED"
    strategies = {a["strategy"] for a in p.alternatives}
    assert strategies == {"full_reperformance", "editorial_only", "lipsync_patch"}


def test_make_him_more_skeptical_with_nothing_selected_changes_the_whole_video() -> None:
    """Audit NL-02: the Studio's placeholder instruction typed with no scene ticked failed with
    "no @selection in this edit" — the recorded fixture is scoped to a selection; with none, the
    request plans like an unrecorded one (the host, every scene), as in dev and test where the
    template planner is allowed."""
    plan, p = propose("make him more skeptical", allow_template=True)
    assert plan.planner == "template"
    assert op_types(plan) == ["set_acting"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    for scene in p.spec.scenes:
        assert scene.acting is not None
        assert all(s.emotion is not None and s.emotion.displayed.label == "skeptical" for s in scene.acting.states)
    assert touches(generation(p), SCENE_HOOK) and touches(generation(p), SCENE_REVEAL)


def test_make_him_more_skeptical_with_voice_locked_keeps_the_audio() -> None:
    _, p = propose("make him more skeptical", voice_locked=True, selection=Selection(scene_keys=["scn_reveal"]))
    assert p.status == "proposed", p.issues
    assert not touches(runs(p), ("tts.", "asr.", "align."))
    assert touches(generation(p), ("avatar.render:sht_4",))
    assert p.coverage_delta["blocked_by_lock"] == ["voice"]
    assert any(b.get("group") == "voice" for b in p.impact["locks_blocking"])


def test_make_her_smile_less() -> None:
    plan, p = propose("make her smile less")
    assert op_types(plan) == ["set_behavior_event", "set_acting"]
    assert any("only cast member" in a for a in plan.assumptions)  # "her" resolved to the host
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    smile = next(e for s in p.spec.scenes if s.acting for e in s.acting.events if e.type == "small_smile")
    assert smile.intensity == pytest.approx(0.1)
    # the global engine cannot show the smile: changing it has no visible effect …
    unseen = {i["item_ref"] for i in p.coverage_delta["no_visible_effect"]}
    assert "/scenes[scn_reveal]/acting/events[ev_2]" in unseen
    # … while the held-back reaction is realized editorially on the existing punch-in
    reaction = next(
        i
        for i in p.coverage_delta["items"]
        if i["item_ref"] == "/scenes[scn_reveal]/acting/states[st_2]/strategies/reaction"
    )
    assert reaction["before"]["level"] == "UNSUPPORTED"
    assert reaction["after"] == {"level": "APPROXIMATED", "method": "editorial_punch"}
    assert generation(p) == {"avatar.render:sht_4:c1:t1"}  # no new voice, keyframe or plate


def test_change_the_room_to_a_modern_office() -> None:
    plan, p = propose("change the room to a modern office")
    assert op_types(plan) == ["set_world_binding"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    assert {str(s.world.world_version_id) for s in p.spec.scenes if s.world} == {str(ALEX.OFFICE_WORLD_VERSION_ID)}
    assert p.spec.scene("scn_reveal").world.camera_position_key == "cam_desk_front"  # type: ignore[union-attr]
    g = generation(p)
    assert {"world.plate:scn_hook", "world.plate:scn_reveal", "image.keyframe:sht_1", "image.keyframe:sht_4"} <= g
    assert {"audio.room:scn_hook", "audio.room:scn_reveal", "mix.audio:main"} <= runs(p)  # other acoustics
    assert not touches(runs(p), ("tts.", "asr.", "align.", "captions.", "audio.music", "audio.sfx"))
    assert any("el_monitor" in note for note in p.impact["notes"])  # overrides of absent elements dropped


def test_make_the_camera_slightly_handheld() -> None:
    plan, p = propose("make the camera slightly handheld")
    assert op_types(plan) == ["set_camera"]
    assert p.status == "proposed", p.issues
    assert generation(p) == set()  # post only
    assert set(p.impact["regenerate"]) == {"post.camera:sht_1", "post.camera:sht_4"}


def test_change_the_outfit_but_keep_the_face() -> None:
    plan, p = propose("change the outfit but keep the face")
    assert op_types(plan) == ["set_wardrobe", "set_lock"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    assert {"appearance"} <= {lock.group for lock in p.spec.locks}
    worn = {str(c.wardrobe_version_id) for s in p.spec.scenes for c in s.cast}
    assert worn == {str(ALEX.WARDROBE_NAVY_VERSION_ID)}
    assert {"image.keyframe:sht_1", "image.keyframe:sht_4"} <= generation(p)
    assert not touches(runs(p), ("tts.", "world.plate", "behavior.resolve"))


def test_keep_everything_the_same_but_change_the_accent() -> None:
    plan, p = propose("keep everything the same but change the accent")
    assert op_types(plan) == ["set_cast", "set_lock"]
    assert p.status == "proposed", p.issues
    assert p.spec is not None
    assert str(p.spec.cast[0].overrides.voice_version_id) == str(ALEX.VOICE_UK_VERSION_ID)
    assert {lock.group for lock in p.spec.locks} >= {"script", "appearance", "wardrobe", "world", "acting", "camera"}
    g = generation(p)
    assert {"tts.segment:seg_1", "tts.segment:seg_2"} <= g
    assert not touches(g, ("image.keyframe", "world.plate"))


def test_a_locked_voice_refuses_the_accent_change_and_names_the_lock() -> None:
    _, p = propose("keep everything the same but change the accent", voice_locked=True)
    assert p.status == "failed"
    locked = [i for i in p.issues if i.code == "locked"]
    assert locked and locked[0].detail["group"] == "voice"  # type: ignore[index]
    assert "voice" in locked[0].message and "lock" in locked[0].message


def test_an_instruction_cannot_remove_a_lock() -> None:
    data = two_scene_spec_dict()
    spec, parent = _parent(data)
    refs = example_build_refs()
    inputs = ProposalInputs(
        spec, refs, config_bundle(), mock_catalog(), example_references(), lambda _s: refs, parent, actor="director"
    )
    ops = parse_operations([{"op": "set_lock", "remove": [{"group": "voice"}]}])
    p = compute_proposal(inputs, ops)
    assert p.status == "failed" and p.issues[0].code == "lock_removal_forbidden"
    user = ProposalInputs(
        spec,
        refs,
        config_bundle(),
        mock_catalog(),
        example_references(),
        lambda _s: refs,
        parent,
        allow_lock_removal=True,
    )
    assert compute_proposal(user, ops).status == "proposed"


def test_an_environment_lock_refuses_world_edits_in_scope_and_allows_them_elsewhere() -> None:
    data = two_scene_spec_dict()
    data["locks"] = [{"group": "world", "scope": {"scene_keys": ["scn_hook"]}, "set_by": "user"}]
    spec, parent = _parent(data)
    refs = example_build_refs()
    inputs = ProposalInputs(spec, refs, config_bundle(), mock_catalog(), example_references(), lambda _s: refs, parent)
    for raw in (
        {"op": "set_world_binding", "scope": {"scene_keys": ["scn_hook"]}, "time_of_day": "evening"},
        {"op": "set_world_override", "scope": {"scene_keys": ["scn_hook"]}, "element_states": {"el_neon": "off"}},
        {"op": "regenerate", "scope": {"scene_keys": ["scn_hook"]}, "components": ["background"]},
    ):
        p = compute_proposal(inputs, parse_operations([raw]))
        assert p.status == "failed", raw
        assert any("world" in i.message for i in p.issues), raw
    for raw in (
        {"op": "set_world_binding", "scope": {"scene_keys": ["scn_reveal"]}, "time_of_day": "evening"},
        {"op": "set_world_override", "scope": {"scene_keys": ["scn_reveal"]}, "element_states": {"el_neon": "off"}},
    ):
        assert compute_proposal(inputs, parse_operations([raw])).status == "proposed", raw
