"""ctc_aligner (Phase 8): the Viterbi alignment and the word mapping on emissions with a known
answer (the CPU stand-in "speaks" each character in turn). No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.align import ctc_viterbi
from ce_plugin_kit.media import ffmpeg
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_asr_ctc.adapter import word_spans
from ce_plugin_asr_ctc.testing import FakeCTCBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_viterbi_needs_enough_frames() -> None:
    with pytest.raises(ValueError, match="frames"):
        ctc_viterbi(np.zeros((2, 4)), [1, 1, 2])


def test_word_spans_cover_the_words_in_order_and_interpolate_empty_ones(tmp_path: Path) -> None:
    wav = tmp_path / "two_seconds.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", str(wav))
    backend = FakeCTCBackend(script=["iyi", "günler"])
    lp, frame_s = backend.emissions(str(wav))
    tokens = backend.encode_words(["iyi", "—", "günler"], "tr")
    spans = word_spans(lp, tokens, blank=0, separator=1, frame_s=frame_s, duration_s=2.0)
    assert len(spans) == 3
    (a_start, a_end, a_conf), (b_start, b_end, b_conf), (c_start, c_end, _) = spans
    assert a_start < a_end <= b_start <= b_end <= c_start < c_end <= 2.0
    assert b_conf == 0.3 and a_conf > 0.9  # the punctuation-only word has no tokens: interpolated


def test_align_turkish_onto_canonical_words(tmp_path: Path) -> None:
    async def go() -> m.AlignResult:
        ctx = LocalRunContext(tmp_path / "ctx", seed=1)
        media = await make_media(ctx, tmp_path, audio="speech")
        adapter = await load_with_backend(
            REGISTRY.get("ctc_aligner"), FakeCTCBackend(script=["iyi", "günler", "dostlar"]), tmp_path
        )
        request = m.AlignRequest(
            audio=media["audio"], text="İyi günler, dostlar!", words=["İyi", "günler,", "dostlar!"], language="tr",
            spoken_words=["iyi", "günler", "dostlar"], spoken_sources=[0, 1, 2],
        )  # fmt: skip
        return await adapter.run("asr.align", request, ctx)  # type: ignore[no-any-return]

    result = asyncio.run(go())
    assert result.precision == "fine" and [w.word for w in result.words] == ["İyi", "günler,", "dostlar!"]
    starts = [w.start_s for w in result.words]
    assert starts == sorted(starts) and starts[0] < 0.5 and result.words[-1].end_s <= 2.0
    assert all(w.confidence > 0.9 for w in result.words)
