"""Model-based QC metric adapters (Phase 8): SyncNet face crops and the not-measurable paths, UVQ
portrait handling and frame statistics, UTMOS refusal of silence, on the CPU stand-ins. The
numbers are synthetic; nothing here validates a metric (rule 5)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import ffmpeg
from ce_testing.engines import load_with_backend, make_media

from ce_plugin_qc_gpu.faces import crop_face, fill_gaps, median_filter
from ce_plugin_qc_gpu.testing import FakeSyncNet, FakeUTMOS, FakeUVQ

REGISTRY = discover(app_env="test", include_mocks=True)


def test_median_filter_and_gaps() -> None:
    assert list(median_filter([1, 9, 1, 1, 1], 3)) == [1, 1, 1, 1, 1]
    assert list(median_filter([4, 4], 5)) == [0, 0]  # zero-padded edges, like scipy.signal.medfilt
    assert fill_gaps([None, (1, 1, 2, 2), None]) == [(1, 1, 2, 2)] * 3 and fill_gaps([None]) is None


def test_crop_geometry() -> None:
    frame = np.zeros((400, 300, 3), np.uint8)
    frame[150:250, 100:200] = 255
    crop = crop_face(frame, 150, 200, 50)
    assert crop.shape == (224, 224, 3)
    assert int(crop[112, 112].mean()) == 255  # the face sits in the crop's middle


async def _run(tmp: Path, plugin: str, capability: str, backend: Any, kind: str) -> m.QCMetricResult:
    ctx = LocalRunContext(tmp / "ctx", seed=1)
    media = await make_media(ctx, tmp, item=kind)
    adapter = await load_with_backend(REGISTRY.get(plugin), backend, tmp)
    return await adapter.run(capability, m.QCMetricRequest(media=media["item"]), ctx)  # type: ignore[no-any-return]


def test_syncnet_crops_every_frame_and_stays_advisory(tmp_path: Path) -> None:
    backend = FakeSyncNet()
    result = asyncio.run(_run(tmp_path, "syncnet_v1", "qc.lipsync", backend, "video"))
    assert result.advisory and result.passed is None and result.metrics["measurable"] == 1.0
    assert backend.crops is not None and backend.crops.shape == (25, 224, 224, 3)
    assert result.metrics["offset_frames"] == 0.0 and result.score == 6.5


def test_syncnet_without_face_is_not_measurable(tmp_path: Path) -> None:
    result = asyncio.run(_run(tmp_path, "syncnet_v1", "qc.lipsync", FakeSyncNet(faceless=True), "video"))
    assert (
        result.metrics["measurable"] == 0.0 and result.metrics["unmeasurable_no_face"] == 1.0 and result.passed is None
    )


def test_uvq_transposes_portrait_and_reports_frames(tmp_path: Path) -> None:
    backend = FakeUVQ()
    result = asyncio.run(_run(tmp_path, "uvq_15", "qc.vqa", backend, "video"))
    assert backend.calls == [(1, True, 1)]  # 360×640 is portrait
    assert result.metrics["frames"] == 1.0 and result.score == 3.5 and not result.advisory


def test_utmos_scores_speech_and_refuses_silence(tmp_path: Path) -> None:
    result = asyncio.run(_run(tmp_path, "utmos_v2", "qc.speech_quality", FakeUTMOS(), "speech"))
    assert result.score == 3.9 and result.metrics["utmos"] == 3.9

    async def silent() -> m.QCMetricResult:
        ctx = LocalRunContext(tmp_path / "s", seed=1)
        path = tmp_path / "silence.wav"
        ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "2", str(path))
        ref = await ctx.put_file(path, "audio")
        adapter = await load_with_backend(REGISTRY.get("utmos_v2"), FakeUTMOS(), tmp_path / "s")
        return await adapter.run("qc.speech_quality", m.QCMetricRequest(media=ref), ctx)  # type: ignore[no-any-return]

    quiet = asyncio.run(silent())
    assert quiet.metrics["measurable"] == 0.0 and quiet.passed is None
