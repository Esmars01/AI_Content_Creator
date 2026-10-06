"""vLLM VLM adapter (Phase 8): the answer envelope, instructions, window cutting and resampling, and
the answer/confidence unpacking on the CPU stand-in. No model runs (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_vision_vllm.adapter import envelope, instructions
from ce_plugin_vision_vllm.testing import FakeVLMBackend, example_for

REGISTRY = discover(app_env="test", include_mocks=True)
EVENTS = {
    "type": "object",
    "properties": {
        "events": {"type": "array", "items": {"type": "object", "properties": {"t_s": {"type": "number"}}}},
        "summary": {"type": "string"},
        "verdict": {"enum": ["pass", "fail"]},
    },
    "required": ["events", "summary", "verdict"],
}


def test_envelope_and_example() -> None:
    env = envelope(EVENTS)
    assert env["required"] == ["answer", "confidence"] and env["properties"]["answer"] == EVENTS
    assert envelope({})["properties"]["answer"]["properties"] == {"text": {"type": "string"}}
    assert example_for(EVENTS) == {"events": [], "summary": "", "verdict": "pass"}


def test_instructions_name_the_window() -> None:
    text = instructions("Does the speaker look at the camera?", "video", (1.0, 3.5))
    assert "1.00–3.50 s window" in text and text.endswith("Does the speaker look at the camera?")


@pytest.mark.parametrize("plugin_id", ["vlm_qwen36_35b_a3b", "vlm_qwen38_27b"])
def test_video_window_is_cut_and_resampled(plugin_id: str, tmp_path: Path) -> None:
    async def go() -> tuple[m.VisionResult, FakeVLMBackend]:
        ctx = LocalRunContext(tmp_path / "ctx", seed=2)
        media = await make_media(ctx, tmp_path, clip="video")
        backend = FakeVLMBackend()
        adapter = await load_with_backend(REGISTRY.get(plugin_id), backend, tmp_path)
        request = m.VisionRequest(
            media=media["clip"], question="what happens?", json_schema=EVENTS, sampling_fps=4, window_s=(0.25, 0.75)
        )
        return await adapter.run("vision.video", request, ctx), backend  # type: ignore[return-value]

    result, backend = asyncio.run(go())
    assert result.answer == {"events": [], "summary": "", "verdict": "pass"} and result.confidence == 0.1
    kind, clip, _ = backend.calls[0]
    info = probe(clip)
    assert kind == "video" and abs(info.duration_s - 0.5) < 0.15 and abs(info.fps - 4.0) < 0.01 and not info.has_audio


def test_image_question_and_bad_window(tmp_path: Path) -> None:
    async def go(**kw: Any) -> m.VisionResult:
        ctx = LocalRunContext(tmp_path / "ctx", seed=2)
        media = await make_media(ctx, tmp_path, still="image", clip="video")
        adapter = await load_with_backend(REGISTRY.get("vlm_qwen36_35b_a3b"), FakeVLMBackend(), tmp_path)
        if kw:
            return await adapter.run("vision.video", m.VisionRequest(media=media["clip"], question="q", **kw), ctx)  # type: ignore[no-any-return]
        return await adapter.run(
            "vision.image", m.VisionRequest(media=media["still"], question="is there a face?"), ctx
        )  # type: ignore[no-any-return]

    assert asyncio.run(go()).answer == {"text": ""}
    with pytest.raises(ValueError, match="empty window"):
        asyncio.run(go(window_s=(0.5, 0.5)))
