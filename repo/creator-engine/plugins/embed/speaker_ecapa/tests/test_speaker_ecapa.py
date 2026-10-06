"""Speaker embedding adapter (Phase 8): speech-only selection from word timings, normalization and
the minimum-speech refusal on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import ffmpeg
from ce_testing.engines import load_with_backend

from ce_plugin_embed_speaker.adapter import speech_only
from ce_plugin_embed_speaker.testing import FakeEcapaBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def _w(i: int, s: float, e: float) -> m.AlignedWord:
    return m.AlignedWord(index=i, word=f"w{i}", start_s=s, end_s=e)


def test_speech_only_merges_padded_spans() -> None:
    samples = np.arange(16_000 * 3, dtype=np.float32)
    out = speech_only(samples, [_w(0, 0.5, 0.8), _w(1, 0.85, 1.0), _w(2, 2.0, 2.2)])
    assert len(out) == int(0.6 * 16_000) + int(0.3 * 16_000)
    assert len(speech_only(samples, [])) == len(samples)


def _embed(tmp: Path, freq: int, seconds: float, **kw: object) -> m.EmbeddingResult:
    async def go() -> m.EmbeddingResult:
        ctx = LocalRunContext(tmp / f"ctx{freq}", seed=1)
        path = tmp / f"voice{freq}.wav"
        ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}:sample_rate=48000", str(path))
        ref = await ctx.put_file(path, "audio")
        adapter = await load_with_backend(REGISTRY.get("speaker_ecapa"), FakeEcapaBackend(), tmp)
        return await adapter.run("voice.embed", m.AudioAnalysisRequest(audio=ref, **kw), ctx)  # type: ignore[no-any-return, arg-type]

    return asyncio.run(go())


def test_embedding_is_unit_length_and_discriminates(tmp_path: Path) -> None:
    a1, a2, b = _embed(tmp_path, 180, 2.0), _embed(tmp_path / "x", 180, 2.5), _embed(tmp_path, 900, 2.0)
    va, va2, vb = (np.asarray(r.vectors[0]) for r in (a1, a2, b))
    assert a1.dim == 192 and abs(float(np.linalg.norm(va)) - 1.0) < 1e-4
    assert float(va @ va2) > float(va @ vb)


def test_too_little_speech_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 1 s"):
        _embed(tmp_path, 180, 2.0, word_timings=[_w(0, 0.1, 0.4)])
