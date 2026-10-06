"""MediaPipe face and body analyzers (§16.2, Phase 7).

- Synthetic: a drawn face is found and tracked; a frame without a face gives no measurements (the
  honesty rule: NOT_MEASURABLE, never a guess). Needs the MediaPipe task files (`make
  fetch-cpu-assets`); skips with that reason otherwise.
- Fixture clips: owner-supplied clips with labelled events (`eval/fixtures/analyzer_clips`, consented
  or permissively licensed, §16.7). Set `FIXTURE_CLIPS_DIR` to their directory; without them the
  test skips with that reason (none are committed).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.manifest import missing_assets
from ce_contracts.plugins import discover

ROOT = Path(__file__).resolve().parents[4]
CACHE = Path(os.environ.get("CE_TEST_MODEL_CACHE", ROOT / ".cache" / "models"))
FIXTURES = os.environ.get("FIXTURE_CLIPS_DIR") or ""
HAVE_FIXTURES = bool(FIXTURES) and (Path(FIXTURES) / "manifest.yaml").is_file()
REGISTRY = discover(app_env="test", include_mocks=True)


def _require(*plugin_ids: str) -> None:
    for plugin_id in plugin_ids:
        missing = missing_assets(REGISTRY.get(plugin_id).manifest, CACHE)
        if missing:
            pytest.skip(f"{plugin_id}: assets missing under {CACHE} ({missing[0]}); run `make fetch-cpu-assets`")


async def _adapter(plugin_id: str, tmp: Path) -> Any:
    adapter = REGISTRY.get(plugin_id).adapter()
    await adapter.load(LoadContext(model_cache_dir=str(CACHE), scratch_dir=str(tmp / plugin_id), app_env="test"))
    return adapter


async def _clip(tmp: Path, *, figure: bool, seconds: float = 2.0) -> Path:
    """A clip of the mock engines' drawn presenter (or the empty room)."""
    from ce_plugins_mock._media import draw_face_image
    from ce_render.ffmpeg import run_ffmpeg

    frame = tmp / ("face.png" if figure else "room.png")
    draw_face_image(frame, width=540, height=960, seed=3, lines=[], figure=figure)
    out = tmp / ("face.mp4" if figure else "room.mp4")
    await run_ffmpeg(
        [
            "-loop",
            "1",
            "-i",
            str(frame),
            "-t",
            f"{seconds}",
            "-r",
            "10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(out),
        ]
    )
    return out


def test_face_found_and_not_found(tmp_path: Path) -> None:
    _require("mediapipe_face", "mediapipe_body")

    async def run() -> tuple[Any, Any, Any]:
        face = await _adapter("mediapipe_face", tmp_path)
        ctx = LocalRunContext(tmp_path / "ctx")
        with_face = await ctx.put_file(await _clip(tmp_path, figure=True), "video")
        without = await ctx.put_file(await _clip(tmp_path, figure=False), "video")
        found = await face.run("face.landmarks", m.MediaAnalysisRequest(media=with_face, sample_hz=5.0), ctx)
        empty = await face.run("face.landmarks", m.MediaAnalysisRequest(media=without, sample_hz=5.0), ctx)
        boxes = await face.run("face.detect", m.MediaAnalysisRequest(media=with_face, sample_hz=5.0), ctx)
        return found, empty, boxes

    found, empty, boxes = asyncio.run(run())
    assert found.face_detected_ratio >= 0.8
    assert found.series["gaze_deviation_deg"] and all(np.isfinite(found.series["head_yaw_deg"]))
    assert empty.face_detected_ratio == 0.0
    assert empty.series == {} and empty.events == []  # nothing measured, nothing guessed
    first = next(f for f in boxes.frames if f["boxes"])
    x, _y, w, _h, score = first["boxes"][0]
    assert 0.2 < x + w / 2 < 0.8 and score > 0.5


@pytest.mark.skipif(
    not HAVE_FIXTURES,
    reason="owner-supplied analyzer fixture clips are not present: FIXTURE_CLIPS_DIR with a manifest.yaml "
    "(eval/fixtures/analyzer_clips/README.md; consented or permissively licensed clips only, §16.7)",
)
def test_labelled_events_on_fixture_clips(tmp_path: Path) -> None:
    """Every labelled clip: the face is tracked (≥ 80% of samples) and each proxy finds at least
    half of its labelled events (the calibration measures the exact reliability)."""
    _require("mediapipe_face", "mediapipe_body")
    from ce_behavior.calibration import LabelledEvent, proxy_scores
    from ce_config.loader import load_config

    root = Path(FIXTURES)
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    bundle = load_config(ROOT / "config", "test")

    async def run() -> list[tuple[dict[str, list[float]], float, float, list[LabelledEvent]]]:
        face = await _adapter("mediapipe_face", tmp_path)
        body = await _adapter("mediapipe_body", tmp_path)
        ctx = LocalRunContext(tmp_path / "ctx")
        clips = []
        for clip in manifest["clips"]:
            ref = await ctx.put_file(root / clip["file"], "video")
            request = m.MediaAnalysisRequest(media=ref, sample_hz=10.0)
            f = await face.run("face.landmarks", request, ctx)
            b = await body.run("body.landmarks", request, ctx)
            assert f.face_detected_ratio >= 0.8, (clip["file"], f.face_detected_ratio)
            series = {**f.series, **b.series}
            events = [
                LabelledEvent(str(e["proxy"]), float(e["start_s"]), float(e["end_s"])) for e in clip.get("events", [])
            ]
            clips.append((series, 10.0, max(len(v) for v in series.values()) / 10.0, events))
        return clips

    clips = asyncio.run(run())
    labelled = {e.proxy for *_, events in clips for e in events}
    assert labelled, "the fixture manifest labels no events"
    for key in sorted(labelled):
        tp, _fp, fn, _tn = proxy_scores(key, bundle.vocab.proxies[key], clips, bundle.app.behavior.judge)
        assert tp >= (tp + fn) / 2, f"{key}: {tp} of {tp + fn} labelled events found"
