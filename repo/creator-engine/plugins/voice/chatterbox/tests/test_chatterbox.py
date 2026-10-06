"""Chatterbox adapters (Phase 8): translator goldens [3, 8], parameter mapping, pauses, tags,
conditioning bundles and the adapter code on the CPU stand-in. Nothing here runs a model
(`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
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

from ce_plugin_voice_chatterbox.testing import FakeChatterboxBackend

GOLDEN = Path(__file__).parent / "golden"
REGISTRY = discover(app_env="test", include_mocks=True)
IDS = ["chatterbox", "chatterbox_turbo", "chatterbox_multilingual"]


def _request(directives: Any = None, **kw: Any) -> m.TTSRequest:
    base: dict[str, Any] = {
        "text": "Most people get this wrong, honestly.",
        "words": ["Most", "people", "get", "this", "wrong,", "honestly."],
        "language": "en-US",
    }
    return m.TTSRequest(behavior=directives, **{**base, **kw})


def _engine(adapter_id: str, directives: Any) -> dict[str, Any]:
    translator = REGISTRY.get(adapter_id).translator()
    assert translator is not None
    return translator.translate(directives, _request(directives)).engine  # type: ignore[attr-defined, no-any-return]


@pytest.mark.behavior
@pytest.mark.parametrize("adapter_id", IDS)
@pytest.mark.parametrize("case", ["example_seg_2", "laugh"])
def test_golden_translation_and_completeness(adapter_id: str, case: str) -> None:
    directives = (
        example_directives(adapter_id, "seg_2")
        if case == "example_seg_2"
        else voice_directives(nonverbal=[{"tag": "laugh", "after_word": 3}], rate=1.3)
    )
    engine = _engine(adapter_id, directives)
    assert_translation_complete(directives, engine)
    version = REGISTRY.get(adapter_id).translator().version  # type: ignore[union-attr]
    assert_golden(GOLDEN / f"{version}.{case}.json", {"translator": version, "engine": engine})


def test_energy_maps_onto_exaggeration_and_cfg_except_on_turbo() -> None:
    calm = _engine("chatterbox", voice_directives().model_copy(update={}))
    assert 0.3 <= calm["exaggeration"] <= 0.9 and calm["cfg_weight"] == 0.5  # energy 0.4: CFG untouched
    turbo = _engine("chatterbox_turbo", voice_directives())
    assert "exaggeration" not in turbo and "cfg_weight" not in turbo
    energy = next(e for e in turbo["unsupported"] if e["dimension"] == "prosody_energy")
    assert energy["reason"] == "unsupported"


def test_rate_is_a_clamped_time_stretch_and_laughs_are_turbo_tags() -> None:
    directives = voice_directives(nonverbal=[{"tag": "laugh", "after_word": 3}], rate=1.3)
    turbo = _engine("chatterbox_turbo", directives)
    assert turbo["tempo"] == 1.1 and turbo["tags"] == {"3": "[laugh]"}
    rate = next(e for e in turbo["encoded"] if e["dimension"] == "prosody_rate")
    assert rate["clamped"] is True
    base = _engine("chatterbox", directives)
    assert base["tags"] == {}
    laugh = next(e for e in base["unsupported"] if e["dimension"] == "nonverbal_audio")
    assert laugh["reason"] == "unsupported:no_tag_for_this_sound"


def test_tts_inserts_pauses_and_resamples_to_the_request(tmp_path: Path) -> None:
    plugin = REGISTRY.get("chatterbox_turbo")

    async def go() -> tuple[m.TTSResult, FakeChatterboxBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=21)
        backend = FakeChatterboxBackend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        directives = voice_directives(
            nonverbal=[{"tag": "laugh", "after_word": 3}], pauses=[{"after_word": 1, "ms": 500}]
        )
        result = await adapter.run("voice.tts", _request(directives), ctx)
        return result, backend, ctx  # type: ignore[return-value]

    result, backend, ctx = asyncio.run(go())
    texts = [text for text, _ in backend.calls]
    assert texts == ["Most people", "get this [laugh] wrong, honestly."]  # split after the pause, tag inline
    assert [o["seed"] for _, o in backend.calls] == [21, 22]
    info = probe(asyncio.run(ctx.read_artifact(result.audio)))
    assert result.sample_rate == 48_000 and info.sample_rate == 48_000
    assert result.duration_s > 0.5 and result.audio.meta["chunks"] == 2


def test_voice_prepare_bundles_the_reference_and_conditionals(tmp_path: Path) -> None:
    plugin = REGISTRY.get("chatterbox_multilingual")

    async def go() -> tuple[Any, Any, FakeChatterboxBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=3)
        refs = await make_media(ctx, tmp_path, ref="speech")
        backend = FakeChatterboxBackend()
        adapter = await load_with_backend(plugin, backend, tmp_path)
        prepared = await adapter.run(
            "voice.clone_prepare",
            m.VoicePrepareRequest(
                references=[m.VoiceReference(audio=refs["ref"], transcript="hello there", language="tr")]
            ),
            ctx,
        )
        tts = await adapter.run(
            "voice.tts",
            _request(None, language="tr-TR", voice=m.VoiceConditioning(conditioning=prepared.conditioning)),
            ctx,
        )
        return prepared, tts, backend, ctx

    prepared, _, backend, ctx = asyncio.run(go())
    meta, files = read_bundle(asyncio.run(ctx.read_artifact(prepared.conditioning)), tmp_path / "unpacked")
    assert meta["adapter_id"] == "chatterbox_multilingual" and meta["transcript"] == "hello there"
    assert set(files) == {"reference.wav", "conds.pt"}
    (_, options) = backend.calls[0]
    assert options["language_id"] == "tr" and options["conds"].endswith("conds.pt")
