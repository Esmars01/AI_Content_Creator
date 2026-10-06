"""LongCat-Video-Avatar 1.5 adapter (Phase 8): translator goldens [3, 8], window planning and the
adapter code on the CPU stand-in. Nothing here runs the model (`untested_on_gpu`, rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import (
    PLACEHOLDER,
    assert_golden,
    assert_translation_complete,
    example_directives,
    load_with_backend,
    make_media,
    single_state_directives,
)

from ce_plugin_avatar_longcat.params import build_params, segments_for
from ce_plugin_avatar_longcat.testing import FakeLongCatBackend

GOLDEN = Path(__file__).parent / "golden"
PLUGIN = discover(app_env="test", include_mocks=True).get("longcat_avatar")
CASES = {
    "longcat_avatar_v1.example": lambda: example_directives("longcat_avatar", "sht_1:c1"),
    "longcat_avatar_v1.single_state": single_state_directives,
}


def _request(directives: Any = None, **kw: Any) -> m.AvatarRequest:
    base: dict[str, Any] = {"keyframe": PLACEHOLDER, "audio": PLACEHOLDER, "width": 720, "height": 1280, "fps": 25}
    return m.AvatarRequest(behavior=directives, prompt="natural window light", **{**base, **kw})


@pytest.mark.behavior
@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_translation_and_completeness(name: str) -> None:
    translator = PLUGIN.translator()
    assert translator is not None
    directives = CASES[name]()
    engine: Any = translator.translate(directives, _request(directives)).engine  # type: ignore[attr-defined]
    assert_translation_complete(directives, engine)
    assert_golden(GOLDEN / f"{name}.json", {"translator": translator.version, "engine": engine})


def test_a_single_state_prompt_is_descriptive_and_starts_with_the_verbal_cue() -> None:
    directives = single_state_directives()
    engine: Any = PLUGIN.translator().translate(directives, _request(directives)).engine  # type: ignore[union-attr, attr-defined]
    assert engine["prompt"].startswith("A person is speaking. natural window light. Very self-assured")
    assert "small hand gestures" in engine["prompt"]


def test_window_planning() -> None:
    assert segments_for(3.72, 25, 93, 13) == 1  # 93 frames
    assert segments_for(3.8, 25, 93, 13) == 2
    assert segments_for(25.0, 25, 93, 13) == 8  # 93 + 7 × 80 = 653 ≥ 625 frames
    defaults = PLUGIN.manifest.defaults
    p = build_params(width=720, height=1280, audio_seconds=10.0, seed=5, prompt="p", defaults=defaults)
    assert (p.resolution, p.width, p.height, p.segments, p.total_frames) == ("720p", 768, 1280, 3, 253)
    assert (p.steps, p.text_guidance_scale, p.audio_guidance_scale, p.use_distill, p.use_int8) == (
        8,
        1.0,
        1.0,
        True,
        True,
    )
    landscape = build_params(width=832, height=480, audio_seconds=1.0, seed=5, prompt="p", defaults=defaults)
    assert (landscape.resolution, landscape.width, landscape.height) == ("480p", 832, 480)


def test_the_adapter_conforms_the_clip(tmp_path: Path) -> None:
    async def go() -> tuple[Any, FakeLongCatBackend, LocalRunContext]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=3)
        refs = await make_media(ctx, tmp_path, image="image", speech="speech", previous="video")
        backend = FakeLongCatBackend()
        adapter = await load_with_backend(PLUGIN, backend, tmp_path)
        request = _request(
            keyframe=refs["image"], audio=refs["speech"], continuation_frames=refs["previous"], chunk_index=2
        )
        return await adapter.run("avatar.a2v", request, ctx), backend, ctx

    result, backend, ctx = asyncio.run(go())
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert (info.width, info.height, info.has_audio) == (720, 1280, True) and abs(info.duration_s - 2.0) < 0.1
    (params,) = backend.calls
    assert params.segments == 1 and params.prompt == "A person is speaking. natural window light"
