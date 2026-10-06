"""The Creative Director critique (§26): measured scores only, findings with valid edit operations."""

from __future__ import annotations

from ce_core.edit import parse_operations
from ce_qc.critique import SCORE_KEYS, critique
from ce_testing.fixtures import example_spec

SPEC = example_spec().model_dump(mode="json")
SHOTS = {"sht_1": (0.0, 3.0), "sht_2": (3.0, 12.0)}


def test_scores_cover_every_dimension_and_never_invent_values() -> None:
    result = critique(spec=SPEC, shot_times=SHOTS, max_shot_s=5.0)
    assert set(result["scores"]) == set(SCORE_KEYS)
    assert result["scores"]["character_consistency"]["score"] is None  # no consistency report
    assert result["scores"]["realism"]["score"] is None  # no VLM pass
    assert result["scores"]["pacing"]["score"] == 0.5  # one of two shots is longer than the mode allows


def test_findings_propose_operations_that_parse() -> None:
    result = critique(
        spec=SPEC,
        shot_reports=[
            {"verdict": "fail", "shot_key": "sht_1", "ladder": [{"failures": [{"check": "qc.vqa"}]}]},
            {"verdict": "pass", "shot_key": "sht_2", "ladder": []},
        ],
        coverage_entries=[
            {"item_ref": "/scenes[scn_hook]/acting/events[ev_1]", "dimension": "gaze",
             "outcome": "HONORED_NOT_OBSERVED", "expected_for_method": False},
            {"item_ref": "/scenes[scn_hook]/acting/events[ev_2]", "dimension": "smile", "outcome": "HONORED_CONFIRMED"},
        ],
        coverage_targets={"/scenes[scn_hook]/acting/events[ev_1]|gaze": "sht_1:c1"},
        vlm={"scores": {"realism": 0.7}, "problems": [{"t_s": 1.0, "issue": "hand melts", "severity": "critical"}],
             "mock": True},
        shot_times=SHOTS,
        max_shot_s=5.0,
    )  # fmt: skip
    categories = [f["category"] for f in result["findings"]]
    assert categories.count("visual_quality") == 1 and "behavior_believability" in categories
    assert "realism" in categories and "pacing" in categories
    assert result["scores"]["behavior_believability"]["score"] == 0.5
    assert "mock" in result["scores"]["realism"]["basis"]
    for finding in result["findings"]:
        if finding["proposed_ops"]:
            assert parse_operations(finding["proposed_ops"])
        assert finding["evidence"] or finding["category"] == "cta"
