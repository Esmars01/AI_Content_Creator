"""The core editing model (§28, ADR 0025): the closed operation union, SpecPatch, lock checks (§12.7,
I8), anchor rebase (ADR 0004) and the structured spec diff."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from ce_core.edit import (
    EDIT_OPERATION_TYPES,
    PatchError,
    PatchOp,
    SpecPatch,
    apply_patch,
    lock_violations,
    parse_operations,
    rebase_segment,
    regenerate_refusals,
    spec_diff,
    word_map,
)
from ce_core.edit.diff import area_of
from ce_core.spec.videospec import VideoSpec
from ce_core.vocab import load_vocabulary
from ce_testing.fixtures import example_spec_dict
from pydantic import ValidationError

VOCAB = load_vocabulary(Path(__file__).resolve().parents[4] / "config" / "vocab")

SECTION_28 = {
    "set_intent", "set_acting", "add_behavior_event", "remove_behavior_event", "set_behavior_event",
    "add_annotation", "remove_annotation", "set_annotation", "set_world_binding", "set_world_override",
    "set_wardrobe", "set_cast", "set_camera", "set_pacing", "edit_script", "add_scene", "remove_scene",
    "move_scene", "add_shot", "remove_shot", "split_shot", "set_shot", "set_meta", "set_render_outputs",
    "replace_broll", "set_music", "set_sfx", "set_captions", "set_effects", "set_brand", "set_product",
    "set_provenance_label", "regenerate", "reroute", "select_take", "set_lock", "refresh_memory",
    "memory_feedback",
}  # fmt: skip


def test_the_union_is_exactly_the_operations_of_section_28() -> None:
    assert set(EDIT_OPERATION_TYPES) == SECTION_28
    assert len(EDIT_OPERATION_TYPES) == len(SECTION_28)


def test_operations_parse_and_reject_unknown_names_and_fields() -> None:
    ops = parse_operations(
        [
            {
                "op": "set_acting",
                "scope": {"scene_keys": ["scn_hook"]},
                "changes": {"emotion": {"displayed": {"label": "skeptical", "intensity_delta": 0.2}}},
            },
            {"op": "select_take", "shot_key": "sht_1", "take_key": "tk_2"},
        ]
    )
    assert [o.op for o in ops] == ["set_acting", "select_take"]
    with pytest.raises(ValidationError):
        parse_operations([{"op": "make_it_better"}])
    with pytest.raises(ValidationError):
        parse_operations([{"op": "select_take", "shot_key": "sht_1", "take_key": "tk_1", "extra": 1}])
    with pytest.raises(ValidationError):  # both an absolute and a relative intensity
        parse_operations(
            [{"op": "set_acting", "changes": {"emotion": {"displayed": {"intensity": 0.5, "intensity_delta": 0.1}}}}]
        )
    with pytest.raises(ValidationError):  # array indices are never used in keys
        parse_operations([{"op": "remove_scene", "scene_key": "0"}])


# ---------------------------------------------------------------------- patch


def test_patch_sets_adds_and_removes_by_key_without_mutating_its_input() -> None:
    doc = example_spec_dict()
    patch = SpecPatch(
        ops=[
            PatchOp(op="set", path="/scenes[scn_hook]/acting/states[st_2]/emotion/displayed/label", value="skeptical"),
            PatchOp(op="set", path="/scenes[scn_hook]/world/overrides/element_states[el_neon]", value="off"),
            PatchOp(op="add", path="/scenes[scn_hook]/world/overrides/hide_elements[el_mug]", value="el_mug"),
            PatchOp(op="remove", path="/scenes[scn_hook]/acting/events[ev_2]"),
            PatchOp(op="set", path="/generation/seed_overrides[avatar.render:sht_1:c1:t1]", value=7),
            PatchOp(op="remove", path="/scenes[scn_hook]/world/continuity_ref"),
            PatchOp(
                op="add",
                path="/scenes[scn_hook]/acting/events[ev_3]",
                after="ev_1",
                value={**doc["scenes"][0]["acting"]["events"][0], "key": "ev_3"},
            ),
        ]
    )
    out = apply_patch(doc, patch)
    assert doc["scenes"][0]["acting"]["states"][1]["emotion"]["displayed"]["label"] == "serious"  # untouched
    scene = out["scenes"][0]
    assert scene["acting"]["states"][1]["emotion"]["displayed"]["label"] == "skeptical"
    assert scene["world"]["overrides"]["element_states"] == {"el_monitor": "on", "el_neon": "off"}
    assert scene["world"]["overrides"]["hide_elements"] == ["el_mug"]
    assert [e["key"] for e in scene["acting"]["events"]] == ["ev_1", "ev_3"]
    assert out["generation"]["seed_overrides"] == {"avatar.render:sht_1:c1:t1": 7}
    assert scene["world"]["continuity_ref"] is None
    VideoSpec.model_validate(out)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (PatchOp(op="set", path="/scenes[scn_x]/purpose", value="hook"), "no element"),
        (PatchOp(op="add", path="/scenes[scn_hook]/acting/events[ev_1]", value={"key": "ev_1"}), "exists"),
        (PatchOp(op="add", path="/scenes[scn_hook]/acting/events[ev_9]", value={"key": "ev_8"}), "must be"),
        (PatchOp(op="set", path="/scenes[scn_hook]/acting/events[ev_1]", value={"key": "ev_2"}), "must stay"),
        (PatchOp(op="remove", path="/scenes[scn_hook]/acting/events[ev_9]"), "no element"),
        (PatchOp(op="add", path="/meta/title", value="x"), "keyed path"),
    ],
)
def test_patch_errors_name_the_path(change: PatchOp, match: str) -> None:
    with pytest.raises(PatchError, match=match):
        apply_patch(example_spec_dict(), SpecPatch(ops=[change]))


# ---------------------------------------------------------------------- locks (I8)


def _changed(mutate: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    before = example_spec_dict()
    after = example_spec_dict()
    mutate(after)
    return before, after


def test_a_locked_value_cannot_change_and_the_violation_names_the_group() -> None:
    before, after = _changed(
        lambda d: d["cast"][0]["overrides"].update(voice_version_id="0" * 8 + "-0000-7000-8000-" + "0" * 12)
    )
    violations = lock_violations(before, after, before["locks"], VOCAB)
    assert [v.group for v in violations] == ["voice"]
    assert violations[0].path == "/cast[char_alex]/overrides/voice_version_id"
    assert "`voice` lock" in violations[0].message and "char_alex" in violations[0].message


def test_locks_are_scoped_and_out_of_scope_changes_pass() -> None:
    def two_characters_locked_elsewhere(d: dict[str, Any]) -> None:
        d["locks"] = [{"group": "voice", "scope": {"character_keys": ["char_other"]}, "set_by": "user"}]

    before, after = _changed(two_characters_locked_elsewhere)
    after["cast"][0]["voice_prosody"] = {"rate": 1.1, "pitch_semitones": 0.0, "energy": 0.5}
    assert lock_violations(before, after, after["locks"], VOCAB) == []


def test_environment_lock_blocks_world_edits_in_scope_and_allows_them_out_of_scope() -> None:
    world_lock = [{"group": "world", "scope": {"scene_keys": ["scn_hook"]}, "set_by": "user"}]
    for mutate in (
        lambda d: d["scenes"][0]["world"].update(time_of_day="evening"),  # set_world_binding
        lambda d: d["scenes"][0]["world"]["overrides"]["element_states"].update(el_neon="off"),  # set_world_override
        lambda d: d["scenes"][0]["world"]["overrides"]["hide_elements"].append("el_mug"),
        lambda d: d["scenes"][0]["world"].update(continuity_ref=None),
    ):
        before, after = _changed(mutate)
        violations = lock_violations(before, after, world_lock, VOCAB)
        assert violations and {v.group for v in violations} == {"world"}
    elsewhere = [{"group": "world", "scope": {"scene_keys": ["scn_other"]}, "set_by": "user"}]
    before, after = _changed(lambda d: d["scenes"][0]["world"].update(time_of_day="evening"))
    assert lock_violations(before, after, elsewhere, VOCAB) == []


def test_a_new_scene_carries_no_locked_value_but_a_new_segment_changes_locked_wording() -> None:
    world_lock = [{"group": "world", "scope": {}, "set_by": "user"}]
    before = example_spec_dict()
    after = example_spec_dict()
    extra = {**after["scenes"][0], "key": "scn_two", "order": 2, "segment_keys": [], "shots": [], "acting": None}
    after["scenes"].append(extra)
    assert lock_violations(before, after, world_lock, VOCAB) == []
    script_lock = [{"group": "script", "scope": {}, "set_by": "user"}]
    after = example_spec_dict()
    after["script"]["segments"].append({**after["script"]["segments"][1], "key": "seg_3", "annotations": []})
    violations = lock_violations(before, after, script_lock, VOCAB)
    assert [v.path for v in violations] == ["/script/segments[seg_3]/text"]
    before, after = _changed(lambda d: d["script"]["segments"][0].update(text="Everyone says AI agents are chatbots."))
    assert [v.group for v in lock_violations(before, after, script_lock, VOCAB)] == ["script"]


def test_removing_a_scene_with_a_locked_world_is_refused() -> None:
    before, after = _changed(lambda d: d["scenes"].clear())
    violations = lock_violations(before, after, [{"group": "world", "scope": {}, "set_by": "user"}], VOCAB)
    assert violations and "removes" in violations[0].message


def test_a_shot_scoped_camera_lock_pins_that_shot_and_its_scene_camera_position() -> None:
    lock = [{"group": "camera", "scope": {"shot_keys": ["sht_1"]}, "set_by": "user"}]
    before, after = _changed(lambda d: d["scenes"][0]["shots"][0]["camera"].update(framing="close_up"))
    assert [v.path for v in lock_violations(before, after, lock, VOCAB)] == ["/scenes[scn_hook]/shots[sht_1]/camera"]
    before, after = _changed(lambda d: d["scenes"][0]["shots"][1]["camera"].update(framing="close_up"))
    assert lock_violations(before, after, lock, VOCAB) == []
    before, after = _changed(lambda d: d["scenes"][0]["world"].update(camera_position_key="cam_side_wide"))
    assert [v.group for v in lock_violations(before, after, lock, VOCAB)] == ["camera"]


def test_regenerate_is_refused_when_a_blocking_lock_is_active_or_every_input_is_locked() -> None:
    world = [{"group": "world", "scope": {"scene_keys": ["scn_hook"]}, "set_by": "user"}]
    refused = regenerate_refusals(["background"], world, VOCAB, scenes=["scn_hook"])
    assert refused and refused[0].groups == ("world",) and "world lock" in refused[0].message
    assert regenerate_refusals(["background"], world, VOCAB, scenes=["scn_other"]) == []
    every: list[dict[str, object]] = [
        {"group": g, "scope": {}, "set_by": "user"} for g in ("appearance", "wardrobe", "world", "camera")
    ]
    assert regenerate_refusals(["keyframe"], every, VOCAB, seed_policy="new") == []  # a new seed may vary
    same = regenerate_refusals(["keyframe"], every, VOCAB, seed_policy="same")
    assert same and "every input" in same[0].message
    assert regenerate_refusals(["teleport"], [], VOCAB)[0].message.startswith("unknown")


# ---------------------------------------------------------------------- rebase (ADR 0004)


def test_word_map_aligns_tokens() -> None:
    mapping, count = word_map("But here's the thing... they're not.", "But here's the catch... they're not.")
    assert count == 6 and mapping == [0, 1, 2, None, 4, 5]


def test_rebase_moves_anchors_through_insertions_and_keeps_states_tiling() -> None:
    doc = example_spec_dict()
    old = doc["script"]["segments"][1]["text"]  # But here's the thing... they're not.
    new = "But honestly, here's the real thing... they're not."
    out, report = rebase_segment(doc, "seg_2", old, new)
    scene = out["scenes"][0]
    st_2 = scene["acting"]["states"][1]
    assert st_2["span"]["start"]["word"] == 0 and st_2["span"]["end"]["word"] == 7
    ev_1 = scene["acting"]["events"][0]  # was at "the" (2) → 3
    assert ev_1["at"]["word"] == 3
    ev_2 = scene["acting"]["events"][1]  # was at "not" (5) → 7
    assert ev_2["at"]["word"] == 7
    pause = out["script"]["segments"][1]["annotations"][0]  # "thing" (3) → 5
    assert pause["span"]["start"]["word"] == 5 and pause["span"]["end"]["word"] == 5
    assert scene["shots"][0]["span"]["end"]["word"] == 7
    assert {m["path"] for m in report.moved} >= {"/scenes[scn_hook]/acting/events[ev_1]/at"}
    assert report.dropped == []
    seg_1_state = scene["acting"]["states"][0]  # another segment: untouched
    assert seg_1_state == doc["scenes"][0]["acting"]["states"][0]


def test_rebase_drops_elements_whose_words_were_deleted() -> None:
    doc = example_spec_dict()
    out, report = rebase_segment(doc, "seg_2", "But here's the thing... they're not.", "But they're not.")
    assert [a["key"] for a in out["script"]["segments"][1]["annotations"]] == ["an_3"]
    assert sorted(d["path"] for d in report.dropped) == [
        "/scenes[scn_hook]/shots[sht_2]",  # the overlay over "the thing"
        "/script/segments[seg_2]/annotations[an_2]",  # the pause after "thing"
    ]
    assert [s["key"] for s in out["scenes"][0]["shots"]] == ["sht_1"]
    assert out["scenes"][0]["acting"]["events"][1]["at"]["word"] == 2  # "not"


# ---------------------------------------------------------------------- diff


def test_structured_diff_is_key_addressed() -> None:
    before = example_spec_dict()
    after = example_spec_dict()
    after["version_id"] = "00000000-0000-7000-8000-000000000999"
    after["scenes"][0]["acting"]["states"][1]["emotion"]["displayed"]["intensity"] = 0.8
    after["scenes"][0]["acting"]["events"].reverse()  # order of keyed elements is not a change
    after["scenes"][0]["acting"]["events"].pop(0)
    entries = spec_diff(before, after)
    assert [(e.path, e.kind) for e in entries] == [
        ("/scenes[scn_hook]/acting/states[st_2]/emotion/displayed/intensity", "changed"),
        ("/scenes[scn_hook]/acting/events[ev_2]", "removed"),
    ]
    assert area_of(entries[0].path) == "acting"
    assert area_of("/scenes[scn_hook]/world/overrides") == "world"
    assert area_of("/scenes[scn_hook]/shots[sht_1]/camera/moves") == "camera"
    assert area_of("/cast[char_alex]/overrides/voice_version_id") == "cast"
