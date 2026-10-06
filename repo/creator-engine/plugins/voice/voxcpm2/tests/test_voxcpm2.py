"""VoxCPM2 adapter (Phase 8): translator goldens [3, 8], the "(instruction)text" syntax from
vocabulary descriptions, cloning modes, languages and the designed-voice fallback on the CPU
stand-in. Nothing here runs a model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.behavior import DirectiveRealization
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_plugin_kit.speech import read_bundle
from ce_testing.engines import (
    assert_golden,
    assert_translation_complete,
    example_directives,
    load_with_backend,
    make_media,
    voice_directives,
)

from ce_plugin_voice_voxcpm2.adapter import check_language
from ce_plugin_voice_voxcpm2.backend import compose_text
from ce_plugin_voice_voxcpm2.testing import FakeVoxCPM2Backend
from ce_plugin_voice_voxcpm2.translator import MAX_INSTRUCTION_CHARS

GOLDEN = Path(__file__).parent / "golden"
REGISTRY = discover(app_env="test", include_mocks=True)


def _request(directives: Any = None, **kw: Any) -> m.TTSRequest:
    base: dict[str, Any] = {
        "text": "Most people get this wrong, honestly.",
        "words": ["Most", "people", "get", "this", "wrong,", "honestly."],
        "language": "tr-TR",
    }
    return m.TTSRequest(behavior=directives, **{**base, **kw})


def _instructed(**kw: Any) -> Any:
    """voice_directives with emotion, energy and delivery compiled as text prompts (VoxCPM2's matrix)."""
    directives = voice_directives(**kw)
    text = {"emotion_vocal", "prosody_energy", "delivery"}
    realizations = [
        r.model_copy(update={"method": "text_prompt_segment", "level": "APPROXIMATED"}) if r.dimension in text else r
        for r in directives.realizations
    ]
    return directives.model_copy(update={"realizations": realizations})


def _engine(directives: Any) -> dict[str, Any]:
    translator = REGISTRY.get("voxcpm2").translator()
    assert translator is not None
    return translator.translate(directives, _request(directives)).engine  # type: ignore[attr-defined, no-any-return]


@pytest.mark.behavior
@pytest.mark.parametrize("case", ["example_seg_2", "instructed_whisper"])
def test_golden_translation_and_completeness(case: str) -> None:
    directives = (
        example_directives("voxcpm2", "seg_2")
        if case == "example_seg_2"
        else _instructed(delivery="whisper", nonverbal=[{"tag": "laugh", "after_word": 3}], rate=1.05)
    )
    engine = _engine(directives)
    assert_translation_complete(directives, engine)
    assert_golden(GOLDEN / f"voxcpm2_v1.{case}.json", {"translator": "voxcpm2_v1", "engine": engine})


def test_instruction_comes_from_vocabulary_descriptions_in_a_fixed_order() -> None:
    directives = _instructed(delivery="whisper")
    engine = _engine(directives)
    plan = directives.prosody[0]
    assert engine["instruction"].startswith(plan.descriptions["emotion"])  # emotion, then strategy, then delivery
    assert engine["instruction"].index(plan.descriptions["strategy"][:20]) > 0
    assert len(engine["instruction"]) <= MAX_INSTRUCTION_CHARS
    assert "(" not in engine["instruction"] and ")" not in engine["instruction"]
    encoded = {e["dimension"]: e["control"] for e in engine["encoded"]}
    assert encoded["emotion_vocal"] == encoded["delivery"] == "instruction"
    assert encoded["prosody_rate"] == "time_stretch" and encoded["prosody_pause"] == "inserted_silence"


def test_no_text_realizations_means_no_instruction() -> None:
    engine = _engine(voice_directives())  # emotion as text_prompt_segment only
    only_emotion = {e["dimension"] for e in engine["encoded"] if e["control"] == "instruction"}
    assert only_emotion == {"emotion_vocal"}
    directives = voice_directives()
    plain = directives.model_copy(
        update={"realizations": [r for r in directives.realizations if r.dimension != "emotion_vocal"]}
    )
    assert _engine(plain)["instruction"] == ""


def test_compose_text_and_languages() -> None:
    assert compose_text("Hi.", None, "") == "Hi."
    assert compose_text("Hi.", "a warm voice", "cheerful tone") == "(a warm voice, cheerful tone)Hi."
    assert check_language("tr-TR") == "tr" and check_language("ar") == "ar" and check_language("hi-IN") == "hi"
    with pytest.raises(ValueError, match="does not speak"):
        check_language("uk-UA")


def test_cloning_with_instruction_uses_the_reference_only(tmp_path: Path) -> None:
    plugin = REGISTRY.get("voxcpm2")

    async def go() -> tuple[Any, m.TTSResult, FakeVoxCPM2Backend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=4)
        refs = await make_media(ctx, tmp_path, ref="speech")
        backend = FakeVoxCPM2Backend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        prepared = await adapter.run(
            "voice.clone_prepare",
            m.VoicePrepareRequest(
                references=[m.VoiceReference(audio=refs["ref"], transcript="merhaba", language="tr")]
            ),
            ctx,
        )
        voice = m.VoiceConditioning(conditioning=prepared.conditioning)
        tts = await adapter.run("voice.tts", _request(_instructed(), voice=voice), ctx)
        return prepared, tts, backend, ctx

    prepared, tts, backend, ctx = asyncio.run(go())
    meta, files = read_bundle(asyncio.run(ctx.read_artifact(prepared.conditioning)), tmp_path / "unpacked")
    assert set(files) == {"reference.wav"} and meta["transcript"] == "merhaba"
    (_, options) = backend.calls[0]
    assert (
        options["reference"].endswith("reference.wav") and options["transcript"] is None
    )  # instruction: no continuation
    assert backend.prompts[0].startswith("(") and backend.prompts[0].endswith(")Most people")
    info = probe(asyncio.run(ctx.read_artifact(tts.audio)))
    assert info.sample_rate == 48_000 and tts.audio.meta["chunks"] == 2


def test_cloning_without_instruction_continues_from_the_transcript(tmp_path: Path) -> None:
    plugin = REGISTRY.get("voxcpm2")

    async def go() -> FakeVoxCPM2Backend:
        ctx = LocalRunContext(tmp_path / "ctx", seed=4)
        refs = await make_media(ctx, tmp_path, ref="speech")
        backend = FakeVoxCPM2Backend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        prepared = await adapter.run(
            "voice.clone_prepare",
            m.VoicePrepareRequest(
                references=[m.VoiceReference(audio=refs["ref"], transcript="merhaba", language="tr")]
            ),
            ctx,
        )
        await adapter.run(
            "voice.tts", _request(None, voice=m.VoiceConditioning(conditioning=prepared.conditioning)), ctx
        )
        return backend

    backend = asyncio.run(go())
    (_, options) = backend.calls[0]
    assert options["transcript"] == "merhaba" and backend.prompts[0] == "Most people get this wrong, honestly."


def test_designed_voice_fallback_and_design_candidates(tmp_path: Path) -> None:
    plugin = REGISTRY.get("voxcpm2")

    async def go() -> tuple[FakeVoxCPM2Backend, m.VoiceDesignResult, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=12)
        backend = FakeVoxCPM2Backend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        await adapter.run(
            "voice.tts", _request(_instructed(), voice=m.VoiceConditioning(description="a calm alto")), ctx
        )
        design = await adapter.run(
            "voice.design",
            m.VoiceDesignRequest(description="a calm alto", sample_text="Merhaba", language="tr", count=2),
            ctx,
        )
        return backend, design, ctx  # type: ignore[return-value]

    backend, design, ctx = asyncio.run(go())
    assert backend.prompts[0].startswith("(a calm alto, ")
    assert all(o["seed"] == 7 for _, o in backend.calls)  # a fixed seed keeps the designed timbre across chunks
    assert [d[2] for d in backend.designs] == [12, 13] and len(design.candidates) == 2
    assert probe(asyncio.run(ctx.read_artifact(design.candidates[0]))).sample_rate == 48_000


def test_unknown_realization_methods_are_reported() -> None:
    directives = voice_directives()
    extra = DirectiveRealization(
        item_ref="/scenes[scn_a]/acting/states[st_1]/strategies/gaze",
        dimension="gaze",
        level="UNSUPPORTED",
        method="omit",
    )
    engine = _engine(directives.model_copy(update={"realizations": [*directives.realizations, extra]}))
    assert any(e["dimension"] == "gaze" for e in engine["unsupported"])
