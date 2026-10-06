"""ce_exec units: output documents, editorial context, WER, shot audio, take selection, request inputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from ce_behavior.inputs import editorial_context
from ce_behavior.scene import SceneWords
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_exec.noderun import artifact_refs_in
from ce_exec.outputs import NodeOutput, output_kind
from ce_exec.requests import select_take
from ce_exec.results import word_error_rate
from ce_render.timeline import SegmentAudio, build_timeline, local_shot_audio, shot_audio
from ce_testing.fixtures import example_spec

REF = ArtifactRef(sha256="a" * 64, kind="audio", mime="audio/wav", bytes=3, role="audio")


def test_output_documents_are_canonical_and_ignore_artifact_ids() -> None:
    a = NodeOutput(node_kind="tts.segment", data={"b": 1, "a": [1.0, 2]}, refs={"audio": REF})
    b = NodeOutput(
        node_kind="tts.segment",
        data={"a": [1.0, 2], "b": 1},
        refs={"audio": REF.model_copy(update={"artifact_id": "x"})},
    )
    assert a.encode() == b.encode()
    assert NodeOutput.decode(a.encode()).refs["audio"].sha256 == REF.sha256
    assert output_kind("behavior.resolve") == "cbs" and output_kind("render.final") == "other"
    with pytest.raises((TypeError, ValueError)):  # documents are canonical JSON: text keys only
        NodeOutput(node_kind="x", data={1: "int key"}).encode()  # type: ignore[dict-item]


def test_word_error_rate() -> None:
    assert word_error_rate("But here's the thing... they're not.", "but here's the thing they're not") == 0.0
    assert word_error_rate("one two three four", "one two four") == 0.25
    assert word_error_rate("", "") == 0.0


def test_editorial_context_reads_what_the_spec_already_contains() -> None:
    """The build-time compiler may use only existing editorial elements (§15.7): the fixture's
    overlay (planned as an approximation of ev_1), the punch-in and the scene-long music cue."""
    spec = example_spec()
    scene = spec.scenes[0]
    words = SceneWords.of(spec, scene)
    context = editorial_context(spec, scene, scene.shots[0], words)
    (overlay,) = context.overlays
    assert (overlay.shot_key, overlay.first, overlay.last) == ("sht_2", 10, 11)
    assert overlay.derived_refs == ("/scenes[scn_hook]/acting/events[ev_1]",)
    assert [(p.move_key, p.position, p.kind) for p in context.punches] == [("mv_1", 8, "punch_in")]
    assert context.music_positions == (("mc_1", 0),) and context.captions_emphasis


def test_local_shot_audio_matches_the_timeline() -> None:
    spec = example_spec()
    segments = {
        "seg_1": SegmentAudio(2.9, tuple((0.05 + 0.3 * i, 0.29 + 0.3 * i) for i in range(8))),
        "seg_2": SegmentAudio(2.3, tuple((0.1 + 0.35 * i, 0.4 + 0.35 * i) for i in range(6))),
    }
    timeline = build_timeline(spec, segments)
    globally = shot_audio(timeline, spec, "sht_1")
    locally = local_shot_audio(spec, "sht_1", segments)
    assert locally.duration_s == pytest.approx(globally.duration_s)
    assert [(p.segment_key, p.from_s, p.to_s) for p in locally.pieces] == [
        (p.segment_key, p.from_s, p.to_s) for p in globally.pieces
    ]
    assert [p.at_s for p in locally.pieces] == pytest.approx([p.at_s for p in globally.pieces])
    assert [t for w in locally.words for t in w] == pytest.approx([t for w in globally.words for t in w])


def _run(scores: dict[int, float], takes: list[int], selected: str | None = None) -> Any:
    deps = [f"avatar.render:sht_1:c1:t{t}" for t in takes] + [f"qc.shot:sht_1:t{t}" for t in scores]
    upstream = {f"qc.shot:sht_1:t{t}": NodeOutput(node_kind="qc.shot", data={"score": s}) for t, s in scores.items()}
    shot = SimpleNamespace(takes=SimpleNamespace(selected_take_key=selected), key="sht_1")
    run = SimpleNamespace(node=SimpleNamespace(deps=deps, key="post.expression:sht_1"), upstream=upstream, shot=shot)
    run.deps = lambda prefix: [(k, upstream[k]) for k in deps if k.startswith(prefix) and k in upstream]
    return run


def test_take_selection_prefers_the_user_then_the_best_score() -> None:
    assert select_take(_run({1: 0.4, 2: 0.9}, [1, 2]))[0] == 2  # type: ignore[arg-type]
    assert select_take(_run({1: 0.7, 2: 0.7}, [1, 2]))[0] == 1  # type: ignore[arg-type]
    assert select_take(_run({1: 0.4, 2: 0.9}, [1, 2], selected="tk_1_1"))[0] == 1  # type: ignore[arg-type]
    _, scores = select_take(_run({1: 0.4, 2: 0.9}, [1, 2]))  # type: ignore[arg-type]
    assert scores == {"1": 0.4, "2": 0.9}  # text keys: documents are canonical JSON


def test_request_inputs_are_every_artifact_reference() -> None:
    other = REF.model_copy(update={"sha256": "b" * 64, "kind": "image", "mime": "image/png"})
    request = m.ImageEditRequest(image=other, prompt="x", references=[REF, other], width=8, height=8)
    assert {r.sha256 for r in artifact_refs_in(request)} == {REF.sha256, other.sha256}
