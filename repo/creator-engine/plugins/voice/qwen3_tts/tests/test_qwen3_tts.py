"""Qwen3-TTS adapter (Phase 8): translator goldens [3, 8], language mapping, pauses, the voice-clone
prompt bundle and the designed-voice fallback on the CPU stand-in. Nothing here runs a model
(`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.speech import read_bundle
from ce_testing.engines import (
    assert_golden,
    assert_translation_complete,
    example_directives,
    load_with_backend,
    make_media,
    voice_directives,
)

from ce_plugin_voice_qwen3_tts.adapter import language_name
from ce_plugin_voice_qwen3_tts.testing import FakeQwen3TTSBackend

GOLDEN = Path(__file__).parent / "golden"
REGISTRY = discover(app_env="test", include_mocks=True)


def _request(directives: Any = None, **kw: Any) -> m.TTSRequest:
    base: dict[str, Any] = {
        "text": "Most people get this wrong, honestly.",
        "words": ["Most", "people", "get", "this", "wrong,", "honestly."],
        "language": "de-DE",
    }
    return m.TTSRequest(behavior=directives, **{**base, **kw})


def _engine(directives: Any) -> dict[str, Any]:
    translator = REGISTRY.get("qwen3_tts").translator()
    assert translator is not None
    return translator.translate(directives, _request(directives)).engine  # type: ignore[attr-defined, no-any-return]


@pytest.mark.behavior
@pytest.mark.parametrize("case", ["example_seg_2", "fast_with_laugh"])
def test_golden_translation_and_completeness(case: str) -> None:
    directives = (
        example_directives("qwen3_tts", "seg_2")
        if case == "example_seg_2"
        else voice_directives(nonverbal=[{"tag": "laugh", "after_word": 3}], rate=1.3, delivery="whisper")
    )
    engine = _engine(directives)
    assert_translation_complete(directives, engine)
    assert_golden(GOLDEN / f"qwen3_tts_v1.{case}.json", {"translator": "qwen3_tts_v1", "engine": engine})


def test_the_cloning_model_takes_no_instruction() -> None:
    engine = _engine(voice_directives(delivery="whisper"))
    assert {e["dimension"] for e in engine["encoded"]} == {"prosody_rate", "prosody_pause"}
    reasons = {e["dimension"]: e["reason"] for e in engine["unsupported"]}
    assert reasons["emotion_vocal"] == reasons["delivery"] == "unsupported"


def test_language_names_and_refusal() -> None:
    assert language_name("de-DE") == "German" and language_name("pt-BR") == "Portuguese"
    with pytest.raises(ValueError, match="does not speak"):
        language_name("tr-TR")


def test_prepared_voice_clones_from_the_cached_prompt(tmp_path: Path) -> None:
    plugin = REGISTRY.get("qwen3_tts")

    async def go() -> tuple[Any, m.TTSResult, FakeQwen3TTSBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=5)
        refs = await make_media(ctx, tmp_path, ref="speech")
        backend = FakeQwen3TTSBackend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        prepared = await adapter.run(
            "voice.clone_prepare",
            m.VoicePrepareRequest(
                references=[m.VoiceReference(audio=refs["ref"], transcript="guten Tag", language="de")]
            ),
            ctx,
        )
        voice = m.VoiceConditioning(conditioning=prepared.conditioning)
        tts = await adapter.run("voice.tts", _request(voice_directives(), voice=voice), ctx)
        return prepared, tts, backend, ctx

    prepared, tts, backend, ctx = asyncio.run(go())
    meta, files = read_bundle(asyncio.run(ctx.read_artifact(prepared.conditioning)), tmp_path / "unpacked")
    assert meta["adapter_id"] == "qwen3_tts" and set(files) == {"reference.wav", "prompt.pt"}
    assert [text for text, _ in backend.calls] == ["Most people", "get this wrong, honestly."]
    assert all(o["language"] == "German" and o["prompt"].endswith("prompt.pt") for _, o in backend.calls)
    assert tts.sample_rate == 48_000 and tts.audio.meta["tempo"] == 0.9


def test_without_a_prepared_voice_segments_use_the_description_with_a_fixed_seed(tmp_path: Path) -> None:
    plugin = REGISTRY.get("qwen3_tts")

    async def go() -> FakeQwen3TTSBackend:
        ctx = LocalRunContext(tmp_path / "ctx", seed=9)
        backend = FakeQwen3TTSBackend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        await adapter.run("voice.tts", _request(None, voice=m.VoiceConditioning(description="a warm baritone")), ctx)
        await adapter.run(
            "voice.design",
            m.VoiceDesignRequest(description="a warm baritone", sample_text="Hallo zusammen", language="de", count=2),
            ctx,
        )
        return backend

    backend = asyncio.run(go())
    (_, options) = backend.calls[0]
    assert (
        options["prompt"] is None and options["description"] == "a warm baritone" and options["description_seed"] == 7
    )
    assert [d[2] for d in backend.designs] == [9, 10]
