"""World continuity checks (§19.6): element expectations, relations, scoring and background pairs."""

from __future__ import annotations

from typing import Any

from ce_world.continuity import background_continuity, element_expectations, element_question, score_elements

DNA: dict[str, Any] = {
    "elements": [
        {"key": "el_shelf", "kind": "shelf", "label": "bookshelf", "position": [0.2, 0.9, 0.0], "signature": True},
        {"key": "el_plant", "kind": "plant", "label": "monstera", "position": [0.8, 0.9, 0.0]},
        {"key": "el_lamp", "kind": "lamp", "label": "desk lamp", "position": [0.5, 0.9, 0.0], "default_state": "on",
         "states": ["on", "off"]},
    ],
    "background_layouts": {"cam_front": {"visible_elements": ["el_plant", "el_lamp", "el_shelf"]}},
    "continuity": {"must_show_from": {"cam_front": ["el_shelf"]}},
    # the camera stands near the bottom of the plan looking up (+y): screen-right is +x
    "camera_positions": [{"key": "cam_front", "position": [0.5, 0.1, 1.2], "target": [0.5, 0.9, 1.0]}],
}  # fmt: skip


def test_expectations_list_must_show_and_signature_first_with_left_right_relations() -> None:
    exp = element_expectations(DNA, "cam_front")
    assert exp["elements"][0]["key"] == "el_shelf"
    assert exp["relations"] == [{"left": "el_shelf", "right": "el_lamp"}, {"left": "el_lamp", "right": "el_plant"}]
    question, schema = element_question(exp)
    assert "el_shelf is left of el_lamp" in question
    assert set(schema["properties"]["elements"]["items"]["properties"]["key"]["enum"]) == {
        "el_shelf",
        "el_plant",
        "el_lamp",
    }


def test_scores_count_only_answered_questions_and_list_missing_must_show() -> None:
    exp = element_expectations(DNA, "cam_front")
    answer = {
        "elements": [
            {"key": "el_shelf", "present": False},
            {"key": "el_plant", "present": True},
            {"key": "el_lamp", "present": True, "state_ok": True},
            {"key": "el_unknown", "present": True},
        ],
        "relations": [
            {"left": "el_lamp", "right": "el_plant", "holds": True},
            {"left": "x", "right": "y", "holds": False},
        ],
    }
    scored = score_elements(answer, exp)
    assert scored["missing"] == ["el_shelf"] and scored["present"] == "2/3"
    assert scored["relations_ok"] == "1/1" and scored["states_ok"] == "1/1"
    assert scored["score"] == 0.8
    assert score_elements({"elements": []}, exp)["score"] is None


def test_background_continuity_judges_adjacent_shots_of_the_same_binding_only() -> None:
    shots = [
        {"shot_key": "a", "binding": "w:cam:day", "vector": [1.0, 0.0], "rgb_mean": [100, 100, 100]},
        {"shot_key": "b", "binding": "w:cam:day", "vector": [1.0, 0.1], "rgb_mean": [104, 100, 100]},
        {"shot_key": "c", "binding": "w:other:day", "vector": [0.0, 1.0], "rgb_mean": [10, 10, 10]},
    ]
    result = background_continuity(shots)
    assert [(p["from"], p["to"]) for p in result["pairs"]] == [("a", "b")]
    assert result["score"] is not None and result["score"] > 0.9
    assert background_continuity(shots[:1])["score"] is None
