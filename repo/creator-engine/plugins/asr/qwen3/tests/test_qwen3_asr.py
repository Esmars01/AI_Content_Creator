"""Qwen3-ASR and Qwen3-ForcedAligner adapters (Phase 8): language mapping, transcription with and
without word timestamps, LID, and alignment onto canonical words on the CPU stand-ins. Nothing
here runs a model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_asr_qwen3.languages import code_of, name_of
from ce_plugin_asr_qwen3.testing import FakeQwen3ASRBackend

REGISTRY = discover(app_env="test", include_mocks=True)


def test_language_names() -> None:
    assert name_of("tr-TR") == "Turkish" and name_of("ar") == "Arabic" and name_of("tl") == "Filipino"
    assert name_of("de", aligner=True) == "German"
    with pytest.raises(ValueError, match="ForcedAligner"):
        name_of("tr", aligner=True)
    assert code_of("Chinese,English") == "zh" and code_of("Klingon") == ""


async def _run(tmp: Path, plugin: str, capability: str, backend: Any, make: Any) -> Any:
    ctx = LocalRunContext(tmp / "ctx", seed=1)
    media = await make_media(ctx, tmp, audio="speech")
    adapter = await load_with_backend(REGISTRY.get(plugin), backend, tmp)
    return await adapter.run(capability, make(media["audio"]), ctx)


def test_transcribe_with_timestamps_where_the_aligner_can(tmp_path: Path) -> None:
    backend = FakeQwen3ASRBackend(heard="Most people get this wrong")
    result = asyncio.run(
        _run(tmp_path, "qwen3_asr", "asr.transcribe", backend, lambda a: m.TranscribeRequest(audio=a, language="en-US"))
    )
    assert result.text == "Most people get this wrong" and result.language == "en-US"
    assert [w.word for w in result.words] == ["Most", "people", "get", "this", "wrong"]
    assert all(0 <= w.start_s <= w.end_s <= 2.0 for w in result.words)
    assert backend.calls[0] == ("transcribe", "English")  # no context: the script is never shown


def test_turkish_transcribes_without_timestamps(tmp_path: Path) -> None:
    backend = FakeQwen3ASRBackend(heard="merhaba dünya")
    result = asyncio.run(
        _run(tmp_path, "qwen3_asr", "asr.transcribe", backend, lambda a: m.TranscribeRequest(audio=a, language="tr"))
    )
    assert result.text == "merhaba dünya" and result.words == []
    assert [c[0] for c in backend.calls] == ["transcribe"]


def test_lid_reads_the_detected_language(tmp_path: Path) -> None:
    result = asyncio.run(
        _run(
            tmp_path,
            "qwen3_asr",
            "asr.lid",
            FakeQwen3ASRBackend(heard="hallo", language="German"),
            lambda a: m.LidRequest(audio=a),
        )
    )
    assert result.language == "de" and result.confidence == 0.8
    mixed = asyncio.run(
        _run(
            tmp_path / "b",
            "qwen3_asr",
            "asr.lid",
            FakeQwen3ASRBackend(heard="x", language="Chinese,English"),
            lambda a: m.LidRequest(audio=a),
        )
    )
    assert mixed.language == "zh" and mixed.confidence == 0.4


def test_align_maps_units_onto_canonical_words(tmp_path: Path) -> None:
    backend = FakeQwen3ASRBackend()
    request = lambda a: m.AlignRequest(  # noqa: E731
        audio=a,
        text="Dr. Smith arrived, honestly.",
        words=["Dr.", "Smith", "arrived,", "honestly."],
        language="en",
        spoken_words=["doctor", "smith", "arrived", "honestly"],
        spoken_sources=[0, 1, 2, 3],
    )
    result = asyncio.run(_run(tmp_path, "qwen3_forced_aligner", "asr.align", backend, request))
    assert result.precision == "fine"
    assert [w.word for w in result.words] == ["Dr.", "Smith", "arrived,", "honestly."]
    assert all(w.confidence == 1.0 for w in result.words)
    starts = [w.start_s for w in result.words]
    assert starts == sorted(starts) and result.words[-1].end_s <= 2.0
    assert backend.calls[0] == ("align", ("doctor smith arrived honestly", "English"))


def test_align_refuses_languages_the_aligner_lacks(tmp_path: Path) -> None:
    request = lambda a: m.AlignRequest(audio=a, text="merhaba", words=["merhaba"], language="tr")  # noqa: E731
    with pytest.raises(ValueError, match="does not support"):
        asyncio.run(_run(tmp_path, "qwen3_forced_aligner", "asr.align", FakeQwen3ASRBackend(), request))
