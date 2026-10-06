"""VideoSeal / AudioSeal adapters (Phase 8): payload bits, streamed frame watermarking with the
audio stream kept, the audio layer's mix-rate watermark and both verifications, on the CPU
stand-ins. The stand-ins are `mock_dev`: no test produces a `real` provenance layer (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import probe
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_watermarks.payload import bit_accuracy, bits_of, hex_of
from ce_plugin_watermarks.testing import FakeAudioSealBackend, FakeVideoSealBackend

REGISTRY = discover(app_env="test", include_mocks=True)
PAYLOAD = "ce1:0123456789abcdef01234567"


def test_payload_bits_round_trip() -> None:
    bits = bits_of(PAYLOAD)
    assert len(bits) == 96 and hex_of(bits) == PAYLOAD
    assert list(bits_of(PAYLOAD, 16)) == [0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 1, 1]
    assert bit_accuracy(bits, bits) == 1.0
    assert len(bits_of("p1")) == 96 and hex_of(bits_of("p1")) != "p1"  # hashed: verifiable, not decodable
    with pytest.raises(ValueError):
        bits_of("")


async def _adapter(tmp: Path, plugin: str, backend: Any) -> tuple[Any, LocalRunContext, dict[str, ArtifactRef]]:
    ctx = LocalRunContext(tmp / "ctx", seed=1)
    media = await make_media(ctx, tmp, clip="video")
    return await load_with_backend(REGISTRY.get(plugin), backend, tmp), ctx, media


def test_video_watermark_streams_frames_and_keeps_audio(tmp_path: Path) -> None:
    async def go() -> tuple[m.WatermarkResult, m.VerifyResult, FakeVideoSealBackend, LocalRunContext]:
        backend = FakeVideoSealBackend()
        adapter, ctx, media = await _adapter(tmp_path, "videoseal", backend)
        marked = await adapter.run(
            "provenance.watermark_video", m.WatermarkRequest(media=media["clip"], payload_id=PAYLOAD), ctx
        )
        verify = await adapter.run(
            "provenance.verify", m.VerifyRequest(media=marked.media, layer="watermark_video", payload_id=PAYLOAD), ctx
        )
        return marked, verify, backend, ctx

    marked, verify, backend, ctx = asyncio.run(go())
    assert marked.mode == "mock_dev" and backend.frames_embedded == 25
    info = probe(asyncio.run(ctx.read_artifact(marked.media)))
    assert info.has_audio and (info.width, info.height) == (360, 640)
    assert (
        verify.present
        and verify.payload_id == PAYLOAD
        and verify.detail["bit_accuracy"] == 1.0
        and verify.mode == "mock_dev"
    )


def test_audio_watermark_survives_aac_and_verifies(tmp_path: Path) -> None:
    async def go() -> tuple[m.WatermarkResult, m.VerifyResult, m.VerifyResult]:
        adapter, ctx, media = await _adapter(tmp_path, "audioseal", FakeAudioSealBackend())
        marked = await adapter.run(
            "provenance.watermark_audio", m.WatermarkRequest(media=media["clip"], payload_id=PAYLOAD), ctx
        )
        good = await adapter.run(
            "provenance.verify", m.VerifyRequest(media=marked.media, layer="watermark_audio", payload_id=PAYLOAD), ctx
        )
        clean = await adapter.run(
            "provenance.verify", m.VerifyRequest(media=media["clip"], layer="watermark_audio", payload_id=PAYLOAD), ctx
        )
        return marked, good, clean

    marked, good, clean = asyncio.run(go())
    assert marked.mode == "mock_dev" and marked.media.mime == "audio/mp4"
    assert good.present and good.detail["bit_accuracy"] == 1.0
    assert not clean.present and clean.detail["detection_probability"] == 0.0


def test_another_layer_is_reported_absent(tmp_path: Path) -> None:
    async def go() -> m.VerifyResult:
        adapter, ctx, media = await _adapter(tmp_path, "audioseal", FakeAudioSealBackend())
        return await adapter.run("provenance.verify", m.VerifyRequest(media=media["clip"], layer="c2pa"), ctx)  # type: ignore[no-any-return]

    result = asyncio.run(go())
    assert result.layer == "c2pa" and not result.present and "watermark_audio only" in result.detail["reason"]


def test_bits_shape() -> None:
    assert np.asarray(bits_of(PAYLOAD)).dtype == np.uint8
