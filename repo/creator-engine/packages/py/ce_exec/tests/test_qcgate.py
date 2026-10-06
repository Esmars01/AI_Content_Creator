"""QC gate graph logic (§26): which nodes are inside a shot's QC loop and what a retry re-runs."""

from __future__ import annotations

from ce_build import build_graph
from ce_exec.qcgate import GATED_KINDS, _rerun_set, _targets_for, gate_members
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import example_spec

GRAPH = build_graph(example_spec(), example_build_refs(), config_bundle(), mock_catalog())
BY_KEY = GRAPH.by_key()


def members() -> dict[str, set[str]]:
    return gate_members((n.key, n.kind, list(n.deps), n.shot_key) for n in GRAPH.nodes)


def test_each_shot_loop_holds_its_generators_observation_and_qc() -> None:
    loops = members()
    assert set(loops) == {n.shot_key for n in GRAPH.nodes if n.kind == "qc.shot"}
    talking = loops["sht_1"]
    assert {BY_KEY[k].kind for k in talking} <= GATED_KINDS
    assert {"qc.shot:sht_1:t1", "qc.shot:sht_1:t2", "avatar.render:sht_1:c1:t1", "behavior.observe:sht_1:t2"} <= talking
    assert "image.keyframe:sht_1" in talking
    assert not any(k.startswith(("tts.", "align.", "post.")) for k in talking)


def test_a_failed_take_reruns_only_its_own_nodes() -> None:
    qc = {k for k in members()["sht_1"] if k.startswith("qc.shot:")}
    targets, why = _targets_for(BY_KEY, "sht_1", 1, [{"check": "qc.vqa"}], [])
    assert why is None and targets == {"avatar.render:sht_1:c1:t1"}
    rerun, why = _rerun_set(GRAPH, "sht_1", qc, targets)
    assert why is None
    assert rerun == ["avatar.render:sht_1:c1:t1", "behavior.observe:sht_1:t1", "qc.shot:sht_1:t1"]


def test_a_keyframe_retry_reruns_every_take_of_the_shot() -> None:
    qc = {k for k in members()["sht_1"] if k.startswith("qc.shot:")}
    targets, _ = _targets_for(
        BY_KEY, "sht_1", 1, [], [{"item_ref": "/x", "dimension": "gaze", "rerun": ["image.keyframe", "avatar.render"]}]
    )
    rerun, why = _rerun_set(GRAPH, "sht_1", qc, targets)
    assert why is None and rerun[0] == "image.keyframe:sht_1"
    assert {"qc.shot:sht_1:t1", "qc.shot:sht_1:t2", "avatar.render:sht_1:c1:t2"} <= set(rerun)


def test_voice_targets_are_outside_the_shot_loop() -> None:
    targets, why = _targets_for(
        BY_KEY, "sht_1", 1, [], [{"item_ref": "/x", "dimension": "prosody", "rerun": ["tts.segment", "avatar.render"]}]
    )
    assert targets == set() and why is not None and "tts.segment" in why
