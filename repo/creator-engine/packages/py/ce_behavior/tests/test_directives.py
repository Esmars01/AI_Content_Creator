"""CompiledBehavior → BehaviorDirectives (§15.7): word anchors become seconds relative to the
request's audio, chunks clip and rebase sub-spans, and every realization travels with its level."""

from __future__ import annotations

import pytest
from ce_behavior.directives import item_labels, to_directives
from ce_behavior.scene import SceneWords
from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, catalog_with, example_cbs, version_behavior

pytestmark = pytest.mark.behavior


def _times(words: SceneWords) -> dict[tuple[str, int], tuple[float, float]]:
    return {w: (round(0.1 + 0.33 * i, 3), round(0.38 + 0.33 * i, 3)) for i, w in enumerate(words.order)}


def test_labels_are_vocabulary_tokens() -> None:
    labels = item_labels(example_cbs()["scn_hook"])
    assert labels[("/scenes[scn_hook]/acting/states[st_2]/emotion", "emotion_visual")] == "serious@0.6"
    assert labels[("/scenes[scn_hook]/acting/states[st_2]/strategies/gaze", "gaze")] == "glance_away_and_return"
    assert labels[("/scenes[scn_hook]/acting/events[ev_1]", "gaze")] == "look_away:down_left@0.5"


def test_visual_directives_carry_seconds_and_every_realization() -> None:
    vb = version_behavior(catalog_with(SEGMENT_ONLY))
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == "sht_1:c1")
    directives = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words))
    assert len(directives.realizations) == len(compiled.realizations)
    assert directives.cbs_content_digest == compiled.cbs_content_digest
    spans = directives.visual[0].sub_spans
    event = next(s for s in spans if "/scenes[scn_hook]/acting/events[ev_1]" in s.item_refs)
    assert event.start_s == pytest.approx(0.1 + 0.33 * 10) and event.end_s == pytest.approx(event.start_s + 0.7)
    state = next(s for s in spans if s.labels.get("emotion_visual") == "confident@0.6")
    assert (state.start_s, state.end_s) == (0.1, pytest.approx(0.38 + 0.33 * 7))


def test_a_chunk_window_clips_and_rebases_sub_spans() -> None:
    vb = version_behavior(catalog_with(SEGMENT_ONLY))
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == "sht_1:c1")
    whole = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words))
    second = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words), window=(2.7, 5.0))
    assert all(s.start_s >= 0 for s in second.visual[0].sub_spans)
    serious = next(s for s in second.visual[0].sub_spans if s.labels.get("emotion_visual") == "serious@0.6")
    original = next(s for s in whole.visual[0].sub_spans if s.labels.get("emotion_visual") == "serious@0.6")
    assert serious.start_s == pytest.approx(original.start_s - 2.7, abs=1e-3)


def test_voice_directives_carry_the_prosody_plan() -> None:
    vb = version_behavior(catalog_with(GLOBAL_ONLY))
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == "seg_2")
    directives = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times={})
    (prosody,) = directives.prosody
    assert (prosody.strategy, prosody.rate, prosody.emphasis_words) == ("slow_measured", 0.9, [5])
    assert [p.model_dump() if hasattr(p, "model_dump") else p for p in prosody.pauses] == [{"after_word": 3, "ms": 350}]
    assert directives.visual == []


def test_labels_travel_with_their_vocabulary_descriptions() -> None:
    """Text-prompted translators read the model-agnostic descriptions (config/vocab/descriptions.yaml),
    never invent wording of their own (§15.2, I1)."""
    from ce_config.loader import load_config

    vocab = load_config("config").vocab
    vb = version_behavior(catalog_with(SEGMENT_ONLY))
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == "sht_1:c1")
    directives = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words), vocab=vocab)
    spans = directives.visual[0].sub_spans
    state = next(s for s in spans if s.labels.get("emotion_visual") == "confident@0.6")
    assert state.descriptions["emotion_visual"] == vocab.describe("emotion", "confident").text  # type: ignore[union-attr]
    event = next(s for s in spans if "/scenes[scn_hook]/acting/events[ev_1]" in s.item_refs)
    assert event.descriptions["gaze"] == "briefly looks away from the camera (down and to the left)"
    voice = next(c for c in vb.compiled if c.target_key == "seg_2")
    (prosody,) = to_directives(voice, vb.cbs["scn_hook"], words=words, word_times={}, vocab=vocab).prosody
    assert prosody.descriptions["strategy"].startswith("slower, deliberate pacing")
    described = vocab.describe("emotion", prosody.emotion)  # type: ignore[arg-type]
    assert described is not None and described.vocal is not None
    assert prosody.descriptions["emotion"] == described.vocal  # the voice channel reads the vocal phrasing
    # without a vocabulary nothing is described (and nothing else changes)
    plain = to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words))
    assert all(not s.descriptions for s in plain.visual[0].sub_spans)
