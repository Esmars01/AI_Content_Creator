"""Operation → patch translation (§28 step 3): each handler changes only what its operation names,
validates against the vocabularies and the bound world, and reports what it did outside the spec
(notes, rebase, re-routes, removed locks). Translation never mutates its input."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from ce_core.edit.ops import parse_operations
from ce_core.edit.translate import EditError, TranslateContext, Translation, translate
from ce_core.vocab import load_vocabulary
from ce_testing.fixtures import ALEX, example_spec_dict, home_office_world, modern_office_world

VOCAB = load_vocabulary(Path(__file__).resolve().parents[4] / "config" / "vocab")
HOME = str(ALEX.WORLD_VERSION_ID)
OFFICE = str(ALEX.OFFICE_WORLD_VERSION_ID)
WORLDS = {HOME: home_office_world().model_dump(mode="json"), OFFICE: modern_office_world().model_dump(mode="json")}
NODES = ["avatar.render:sht_1:c1:t1", "avatar.render:sht_1:c1:t2", "video.broll:sht_2:t1", "tts.segment:seg_1"]


def ctx(**kw: Any) -> TranslateContext:
    return TranslateContext(vocab=VOCAB, worlds=WORLDS, node_keys=NODES, **kw)


def run(ops: list[dict[str, Any]], doc: dict[str, Any] | None = None, **kw: Any) -> Translation:
    return translate(parse_operations(ops), doc or example_spec_dict(), ctx(**kw))


def paths(t: Translation) -> list[str]:
    return [op.path for op in t.patch.ops]


def unlocked() -> dict[str, Any]:
    doc = example_spec_dict()
    doc["locks"] = []
    return doc


def test_translation_never_mutates_its_input() -> None:
    doc = example_spec_dict()
    before = copy.deepcopy(doc)
    run([{"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}], doc)
    assert doc == before


def test_an_acting_change_inside_a_state_splits_it_and_changes_only_the_span() -> None:
    span = {"kind": "words", "start": {"segment_key": "seg_1", "word": 0}, "end": {"segment_key": "seg_1", "word": 2}}
    t = run(
        [
            {
                "op": "set_acting",
                "scope": {"scene_keys": ["scn_hook"], "span": span},
                "changes": {"emotion": {"displayed": {"label": "skeptical", "intensity": 0.7}}},
            }
        ]
    )
    states = next(s for s in t.document["scenes"] if s["key"] == "scn_hook")["acting"]["states"]
    assert [s["span"]["start"]["word"] for s in states] == [0, 3, 0]  # st_1 split at word 3; st_2 untouched
    first, second = states[0], states[1]
    assert first["key"] == "st_1" and first["emotion"]["displayed"] == {"label": "skeptical", "intensity": 0.7}
    assert second["emotion"]["displayed"]["label"] == "confident" and second["transition_in"] is None
    assert any("split acting state st_1" in n for n in t.notes)
    assert all(p.startswith(("/scenes[scn_hook]/acting", "/intent")) for p in paths(t))


def test_an_empty_acting_scope_is_an_error() -> None:
    with pytest.raises(EditError) as info:
        run(
            [
                {
                    "op": "set_acting",
                    "scope": {"state_keys": ["st_9"]},
                    "changes": {"strategies": {"gaze": "hold_camera"}},
                }
            ]
        )
    assert info.value.issues[0].code == "empty_scope"


def test_world_overrides_write_only_the_scene_binding_and_never_the_world() -> None:
    worlds_before = copy.deepcopy(WORLDS)
    t = run(
        [
            {
                "op": "set_world_override",
                "scope": {"scene_keys": ["scn_hook"]},
                "element_states": {"el_neon": "off"},
                "hide": ["el_mug"],
                "move_elements": [{"key": "el_mug", "position": [0.1, 0.0, 0.2]}],
            }
        ]
    )
    overrides = t.document["scenes"][0]["world"]["overrides"]
    assert overrides["element_states"] == {"el_monitor": "on", "el_neon": "off"}
    assert overrides["hide_elements"] == ["el_mug"] and overrides["move_elements"][0]["key"] == "el_mug"
    assert all(p.startswith("/scenes[scn_hook]/world/overrides") for p in paths(t))
    assert worlds_before == WORLDS  # I6: World DNA is never touched


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"element_states": {"el_sofa": "on"}}, "unknown_element"),
        ({"element_states": {"el_neon": "flashing"}}, "element_state"),
        ({"move_elements": [{"key": "el_desk", "position": [0, 0, 0]}]}, "not_movable"),
    ],
)
def test_world_overrides_are_checked_against_the_bound_world(override: dict[str, Any], code: str) -> None:
    with pytest.raises(EditError) as info:
        run([{"op": "set_world_override", "scope": {"scene_keys": ["scn_hook"]}, **override}])
    assert info.value.issues[0].code == code


def test_moving_to_another_world_maps_the_binding_and_notes_each_change() -> None:
    t = run([{"op": "set_world_binding", "world_version_id": OFFICE}])
    world = t.document["scenes"][0]["world"]
    assert world["world_version_id"] == OFFICE
    assert world["camera_position_key"] == "cam_desk_front"  # exists in the office too
    assert world["time_of_day"] == "late_afternoon"  # allowed there
    assert world["overrides"]["element_states"] == {}  # el_monitor is not an office element
    assert any("absent from the new world dropped: el_monitor" in n for n in t.notes)
    with pytest.raises(EditError) as info:
        run([{"op": "set_world_binding", "camera_position_key": "cam_ceiling"}])
    assert info.value.issues[0].code == "camera_position"
    with pytest.raises(EditError) as unresolved:
        run([{"op": "set_world_binding", "world_query": "a beach"}])
    assert unresolved.value.issues[0].code == "unresolved_reference"


def test_camera_moves_are_added_at_the_shot_start_and_removed_by_type() -> None:
    t = run([{"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}])
    moves = t.document["scenes"][0]["shots"][0]["camera"]["moves"]
    added = moves[-1]
    assert added["type"] == "handheld_drift" and added["at"] == t.document["scenes"][0]["shots"][0]["span"]["start"]
    removed = run([{"op": "set_camera", "remove_move_types": ["handheld_drift"]}], t.document)
    assert [m["type"] for m in removed.document["scenes"][0]["shots"][0]["camera"]["moves"]] == [
        m["type"] for m in moves[:-1]
    ]
    with pytest.raises(EditError):
        run([{"op": "set_camera", "add_moves": [{"type": "barrel_roll"}]}])


def test_pacing_is_scene_level_and_a_neutral_pacing_is_cleared() -> None:
    t = run([{"op": "set_pacing", "target_wpm_delta": 0.1, "cut_cadence": "fast"}])
    assert t.document["scenes"][0]["pacing"] == {"target_wpm_delta": 0.1, "cut_cadence": "fast"}
    back = run([{"op": "set_pacing", "target_wpm_delta": 0.0, "clear_cut_cadence": True}], t.document)
    assert back.document["scenes"][0]["pacing"] is None


def test_regenerate_writes_new_seeds_for_the_components_nodes_only() -> None:
    doc = unlocked()
    t = run([{"op": "regenerate", "scope": {"shot_keys": ["sht_1"]}, "components": ["avatar_video"]}], doc)
    seeds = t.document["generation"]["seed_overrides"]
    assert set(seeds) == {"avatar.render:sht_1:c1:t1", "avatar.render:sht_1:c1:t2"}
    again = run([{"op": "regenerate", "scope": {"shot_keys": ["sht_1"]}, "components": ["avatar_video"]}], t.document)
    assert all(again.document["generation"]["seed_overrides"][k] != v for k, v in seeds.items())  # a new variation
    same = run([{"op": "regenerate", "scope": {"shot_keys": ["sht_1"]}, "components": ["avatar_video"]}], doc)
    assert same.document["generation"]["seed_overrides"] == seeds  # deterministic
    with pytest.raises(EditError) as director:
        run([{"op": "regenerate", "components": ["acting"]}], doc)
    assert director.value.issues[0].code == "needs_director"


def test_regenerate_refuses_a_component_a_lock_blocks() -> None:
    with pytest.raises(EditError) as info:  # the example locks the voice
        run([{"op": "regenerate", "components": ["voice"]}])
    assert info.value.issues[0].code == "regenerate_refused" and "voice" in info.value.issues[0].message


def test_reroute_and_take_selection_change_the_build_not_the_spec() -> None:
    t = run([{"op": "reroute", "node_keys": ["avatar.render:sht_1:c1:t1"]}])
    assert t.force_reroute == ["avatar.render:sht_1:c1:t1"] and t.patch.ops == []
    pinned = run([{"op": "reroute", "node_keys": ["avatar.render:sht_1:c1:t1"], "adapter_id": "mock_avatar_segment"}])
    assert pinned.document["generation"]["engine_hints"] == {"avatar.render:sht_1:c1:t1": "mock_avatar_segment"}
    with pytest.raises(EditError) as unknown:
        run([{"op": "reroute", "node_keys": ["avatar.render:sht_9:c1:t1"]}])
    assert unknown.value.issues[0].code == "unknown_node"
    take = run([{"op": "select_take", "shot_key": "sht_1", "take_key": "tk_2"}])
    assert paths(take) == ["/scenes[scn_hook]/shots[sht_1]/takes/selected_take_key"]
    with pytest.raises(EditError) as beyond:
        run([{"op": "select_take", "shot_key": "sht_1", "take_key": "tk_3"}])
    assert beyond.value.issues[0].code == "unknown_take"


def test_only_the_user_removes_locks() -> None:
    removal = [{"op": "set_lock", "remove": [{"group": "voice"}]}]
    for actor in ("director", "system", "user"):
        with pytest.raises(EditError) as info:
            run(removal, actor=actor)
        assert info.value.issues[0].code == "lock_removal_forbidden"
    t = run(removal, allow_lock_removal=True)
    assert t.document["locks"] == [] and t.removed_locks[0]["group"] == "voice"
    added = run([{"op": "set_lock", "add": [{"group": "camera", "scope": {"shot_keys": ["sht_1"]}}]}])
    assert added.document["locks"][-1]["group"] == "camera"
    with pytest.raises(EditError):
        run([{"op": "set_lock", "add": [{"group": "everything"}]}])


def test_a_script_edit_rebases_anchors_and_respects_the_script_lock() -> None:
    doc = example_spec_dict()
    t = run(
        [
            {
                "op": "edit_script",
                "segment_key": "seg_1",
                "text": "Honestly, everyone thinks AI agents are just smarter chatbots.",
            }
        ],
        doc,
    )
    (report,) = t.rebase
    assert report["segment_key"] == "seg_1" and report["moved"]
    states = t.document["scenes"][0]["acting"]["states"]
    assert states[0]["span"]["start"]["word"] == 0 and states[0]["span"]["end"]["word"] == 8  # one word longer
    locked = example_spec_dict()
    locked["locks"].append({"group": "script", "scope": {}, "set_by": "user"})
    with pytest.raises(EditError) as info:
        run([{"op": "edit_script", "segment_key": "seg_1", "text": "Something else."}], locked)
    assert info.value.issues[0].code == "wording_locked"


def test_meta_and_shot_removal_are_key_addressed() -> None:
    t = run(
        [
            {"op": "set_meta", "quality_tier": "final"},
            {"op": "remove_shot", "scene_key": "scn_hook", "shot_key": "sht_2"},
        ]
    )
    assert t.document["meta"]["quality_tier"] == "final"
    assert [s["key"] for s in t.document["scenes"][0]["shots"]] == ["sht_1"]
    # the whoosh anchored on the removed shot goes with it, and the proposal says so
    assert set(paths(t)) == {"/meta/quality_tier", "/scenes[scn_hook]/shots[sht_2]", "/audio/sfx[sfx_1]"}
    assert "sound effects anchored at sht_2 removed" in t.notes
