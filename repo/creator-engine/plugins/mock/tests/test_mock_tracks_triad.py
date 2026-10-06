"""Requested vs compiled vs observed [3] (§37), on real mock media: the §11 example compiled for
the segment-control mock, rendered by the mock avatar with injected failures, measured by the mock
observer and judged per take. Failed items are NOT_OBSERVED, performed items CONFIRMED, and items
whose proxy needs an analyzer that does not run (VLM) are NOT_MEASURABLE — never upgraded."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from ce_behavior.directives import to_directives
from ce_behavior.judge import VisualEvidence, judge_visual
from ce_behavior.observe import ChunkMeasurement, observed_behavior
from ce_behavior.scene import SceneWords
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_core.behavior.observed import AnalyzerRef
from ce_testing.behavior import SEGMENT_ONLY, bundle, catalog_with, version_behavior

pytestmark = pytest.mark.behavior

REGISTRY = discover(app_env="test", include_mocks=True)
B = bundle()
EV_1 = "/scenes[scn_hook]/acting/events[ev_1]"
ST_1_GAZE = "/scenes[scn_hook]/acting/states[st_1]/strategies/gaze"
ST_2_GAZE = "/scenes[scn_hook]/acting/states[st_2]/strategies/gaze"


def _ffmpeg(*args: str) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg is not installed")
    subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)


async def _judge_take(tmp: Path, fail_items: list[str]) -> dict[tuple[str, str], Any]:
    ctx = LocalRunContext(tmp, seed=99)
    png, wav = tmp / "key.png", tmp / "speech.wav"
    _ffmpeg("-f", "lavfi", "-i", "color=c=0x406080:s=64x96", "-frames:v", "1", str(png))
    tone = "sine=frequency=180:duration=5:sample_rate=48000"
    _ffmpeg("-f", "lavfi", "-i", tone, "-c:a", "pcm_s16le", "-ac", "1", str(wav))
    keyframe, audio = await ctx.put_file(png, "image"), await ctx.put_file(wav, "audio")

    vb = version_behavior(catalog_with(SEGMENT_ONLY))
    scene = vb.spec.scenes[0]
    words = SceneWords.of(vb.spec, scene)
    times = {w: (round(0.1 + 0.33 * i, 3), round(0.38 + 0.33 * i, 3)) for i, w in enumerate(words.order)}
    compiled = next(c for c in vb.compiled if c.target_key == "sht_1:c1")
    cbs = vb.cbs[scene.key]
    request = m.AvatarRequest(
        keyframe=keyframe,
        audio=audio,
        behavior=to_directives(compiled, cbs, words=words, word_times=times),
        width=64,
        height=96,
        fps=12,
        labels={"fail_items": ",".join(fail_items), "emergent_glances": "off"},
    )
    load = LoadContext(model_cache_dir=str(tmp / "models"), scratch_dir=str(ctx.scratch_dir), app_env="test")
    avatar = REGISTRY.get("mock_avatar_segment").adapter()
    observer = REGISTRY.get("mock_observer").adapter()
    await avatar.load(load)
    await observer.load(load)
    take = await avatar.run("avatar.a2v", request, ctx)
    assert take.behavior_track is not None
    analysis = m.MediaAnalysisRequest(media=take.video, sidecars=[take.behavior_track], sample_hz=10.0)
    face = await observer.run("face.landmarks", analysis, ctx)
    body = await observer.run("body.landmarks", analysis, ctx)
    observed = observed_behavior(
        take_sha256="sha256:" + take.video.sha256,
        character_key="char_alex",
        chunks=[ChunkMeasurement(0.0, float(take.duration_s), face, body)],
        analyzers=[
            AnalyzerRef(capability="face.landmarks", adapter_id="mock_observer", revision="1"),
            AnalyzerRef(capability="body.landmarks", adapter_id="mock_observer", revision="1"),
        ],
    )
    evidence = VisualEvidence(tracks=observed.tracks[0], available=frozenset({"face.landmarks", "body.landmarks"}))
    verdicts: dict[tuple[str, str], Any] = {}
    for control in cbs.requested_controls:
        if B.vocab.dimensions[control.dimension].channel != "visual":
            continue
        item = words.words(control.span)
        start, end = times[item[0]][0], times[item[-1]][1]
        if control.duration_ms:
            end = start + control.duration_ms / 1000.0
        verdicts[(control.item_ref, control.dimension)] = judge_visual(
            control, (start, end), evidence, B.vocab, B.app.behavior.judge
        )
    return verdicts


def test_performed_items_confirm_and_injected_failures_are_not_observed(tmp_path: Path) -> None:
    clean = asyncio.run(_judge_take(tmp_path / "clean", []))
    # ev_1 sits inside st_2, whose glance-away strategy also turns the gaze off the lens: both are
    # dropped so that nothing else on the face can pass for the look-away.
    failed = asyncio.run(_judge_take(tmp_path / "failed", [EV_1, ST_2_GAZE]))
    assert str(clean[(EV_1, "gaze")].verdict) == "CONFIRMED"
    assert clean[(EV_1, "gaze")].measures["look_away_ms"] >= 250
    assert str(clean[(ST_2_GAZE, "gaze")].verdict) == "CONFIRMED"
    assert str(failed[(EV_1, "gaze")].verdict) == "NOT_OBSERVED"
    assert str(failed[(ST_2_GAZE, "gaze")].verdict) == "NOT_OBSERVED"
    assert str(clean[(ST_1_GAZE, "gaze")].verdict) == "CONFIRMED"  # hold_camera: the gaze stays on the lens
    for (ref, dimension), observation in clean.items():
        if dimension == "emotion_visual":
            assert str(observation.verdict) == "NOT_MEASURABLE", ref  # VLM proxies do not run (§16.8)
