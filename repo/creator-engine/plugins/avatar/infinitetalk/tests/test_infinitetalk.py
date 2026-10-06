"""InfiniteTalk adapter (Phase 8): translator goldens [3, 8], generation parameters, and the adapter
code around the engine (continuation, conforming, knobs, low-memory retries) on the CPU stand-in.
Nothing here runs the model: `validation` stays `untested_on_gpu` (rule 5).

Regenerate the golden file after an intended translator change with `CE_UPDATE_GOLDEN=1`."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest
from ce_behavior.directives import to_directives
from ce_behavior.scene import SceneWords
from ce_contracts import models as m
from ce_contracts.behavior import BehaviorDirectives, DirectiveRealization, DirectiveSubSpan, VisualDirectives
from ce_contracts.common import ArtifactRef, LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_plugin_kit.media import ffmpeg, probe
from ce_testing.behavior import bundle, real_engine_catalog, version_behavior

from ce_plugin_avatar_infinitetalk.params import build_params, frames_for
from ce_plugin_avatar_infinitetalk.testing import FakeInfiniteTalkBackend

GOLDEN = Path(__file__).parent / "golden"
PLUGIN = discover(app_env="test", include_mocks=True).get("infinitetalk")


def _times(words: SceneWords) -> dict[tuple[str, int], tuple[float, float]]:
    return {w: (round(0.1 + 0.33 * i, 3), round(0.38 + 0.33 * i, 3)) for i, w in enumerate(words.order)}


def _directives() -> BehaviorDirectives:
    vb = version_behavior(real_engine_catalog({"infinitetalk"}))
    assert vb.routes["avatar.render:sht_1:c1:t1"].adapter_id == "infinitetalk"
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == "sht_1:c1")
    return to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=_times(words), vocab=bundle().vocab)


def _request(directives: BehaviorDirectives | None, **kw: Any) -> m.AvatarRequest:
    ref = ArtifactRef(sha256="0" * 64, kind="image", mime="image/png") if "keyframe" not in kw else kw.pop("keyframe")
    audio = kw.pop("audio", ref)
    defaults: dict[str, Any] = {"width": 720, "height": 1280, "fps": 25, "prompt": "shot on a phone front camera"}
    return m.AvatarRequest(keyframe=ref, audio=audio, behavior=directives, **{**defaults, **kw})


def _single_state_directives() -> BehaviorDirectives:
    """A clip covered by one acting state: its emotion, posture and camera awareness compile to
    `text_prompt_global` (the example version's only talking shot spans two states)."""
    vocab = bundle().vocab
    base = "/scenes[scn_a]/acting/states[st_1]"
    items = [
        (f"{base}/emotion", "emotion_visual", "confident@0.9", vocab.describe("emotion", "confident")),
        (
            f"{base}/strategies/posture",
            "posture",
            "seated_lean_in",
            vocab.describe("strategy.posture", "seated_lean_in"),
        ),
        (
            f"{base}/strategies/camera_awareness",
            "camera_awareness",
            "direct_address",
            vocab.describe("strategy.camera_awareness", "direct_address"),
        ),
    ]
    return BehaviorDirectives(
        cbs_content_digest="sha256:" + "2" * 64,
        target_key="sht_a:c1",
        realizations=[
            *(
                DirectiveRealization(item_ref=r, dimension=d, level="APPROXIMATED", method="text_prompt_global")
                for r, d, _, _ in items
            ),
            DirectiveRealization(
                item_ref=f"{base}/strategies/gaze", dimension="gaze", level="UNSUPPORTED", method="omit"
            ),
        ],
        visual=[
            VisualDirectives(
                shot_key="sht_a",
                character_key="char_a",
                sub_spans=[
                    DirectiveSubSpan(
                        start_s=0.0,
                        end_s=4.0,
                        item_refs=[r for r, _, _, _ in items],
                        labels={d: label for _, d, label, _ in items},
                        descriptions={d: str(text.text) for _, d, _, text in items if text is not None},  # type: ignore[union-attr]
                    )
                ],
            )
        ],
    )


CASES = {"infinitetalk_v1.example": _directives, "infinitetalk_v1.single_state": _single_state_directives}


@pytest.mark.behavior
@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_translation(name: str) -> None:
    translator = PLUGIN.translator()
    assert translator is not None
    directives = CASES[name]()
    out: Any = translator.translate(directives, _request(directives))
    produced = json.loads(json.dumps({"translator": translator.version, "engine": out.engine}, sort_keys=True))
    path = GOLDEN / f"{name}.json"
    if os.environ.get("CE_UPDATE_GOLDEN") == "1":
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert produced == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.behavior
@pytest.mark.parametrize("name", sorted(CASES))
def test_every_directive_is_encoded_or_reported_with_a_reason(name: str) -> None:
    directives = CASES[name]()
    engine = PLUGIN.translator().translate(directives, _request(directives)).engine  # type: ignore[union-attr, attr-defined]
    requested = {(r.item_ref, r.dimension) for r in directives.realizations}
    encoded = {(e["item_ref"], e["dimension"]) for e in engine["encoded"]}
    reported = {(e["item_ref"], e["dimension"]) for e in engine["unsupported"]}
    assert encoded | reported == requested and not encoded & reported
    assert all(e["reason"] for e in engine["unsupported"])
    # prompt phrases come from the vocabulary descriptions, never from engine-specific adjectives
    for entry in engine["encoded"]:
        if entry.get("control") == "text_global":
            assert entry["text"].lower() in engine["prompt"].lower()


def test_a_single_state_clip_becomes_one_descriptive_prompt() -> None:
    directives = _single_state_directives()
    engine = PLUGIN.translator().translate(directives, _request(directives)).engine  # type: ignore[union-attr, attr-defined]
    assert engine["prompt"].startswith("A person is talking. shot on a phone front camera. Very self-assured")
    assert "leaning toward the camera" in engine["prompt"]
    assert {e["control"] for e in engine["encoded"]} == {"text_global"}


def test_a_calibrated_motion_knob_maps_onto_the_audio_cfg_range() -> None:
    directives = BehaviorDirectives(
        cbs_content_digest="sha256:" + "1" * 64,
        target_key="sht_1:c1",
        realizations=[
            DirectiveRealization(item_ref="/x", dimension="head_motion", level="APPROXIMATED", method="omit")
        ],
        visual=[
            VisualDirectives(
                shot_key="sht_1",
                character_key="char_a",
                sub_spans=[DirectiveSubSpan(start_s=0, end_s=2, knobs={"motion_energy": 1.0}, item_refs=["/x"])],
            )
        ],
    )
    engine = PLUGIN.translator().translate(directives, _request(directives)).engine  # type: ignore[union-attr, attr-defined]
    low, high = PLUGIN.manifest.knobs["motion_energy"].range
    assert engine["audio_guide_scale"] == high and low < high
    assert engine["unsupported"] == [{"item_ref": "/x", "dimension": "head_motion", "reason": "unsupported"}]


def test_generation_parameters() -> None:
    defaults = PLUGIN.manifest.defaults
    assert frames_for(3.24, 25) == 81 and frames_for(3.25, 25) == 85 and frames_for(0.01, 25) == 1
    short = build_params(width=480, height=832, audio_seconds=3.0, seed=7, prompt="p", defaults=defaults)
    assert (short.size_bucket, short.mode, short.frame_num, short.max_frames) == ("infinitetalk-480", "clip", 77, 77)
    long = build_params(width=720, height=1280, audio_seconds=24.0, seed=2**40 + 3, prompt="p", defaults=defaults)
    assert (long.size_bucket, long.mode, long.frame_num, long.max_frames) == ("infinitetalk-720", "streaming", 81, 601)
    assert long.seed == (2**40 + 3) % 2**31
    # with the distillation LoRA: 4 steps, text CFG 1, audio CFG 2 (README), no quantization (D90)
    assert (long.steps, long.text_guide_scale, long.audio_guide_scale, long.quant) == (4, 1.0, 2.0, None)
    tuned = build_params(
        width=720,
        height=1280,
        audio_seconds=1,
        seed=1,
        prompt="p",
        defaults=defaults,
        engine={"audio_guide_scale": 2.6},
    )
    assert tuned.audio_guide_scale == 2.6
    low = build_params(width=720, height=1280, audio_seconds=1, seed=1, prompt="p", defaults=defaults, low_memory=True)
    assert (low.num_persistent_param_in_dit, low.t5_cpu) == (0, True)


def _media(tmp: Path) -> tuple[Path, Path, Path]:
    image, audio, prev = tmp / "key.png", tmp / "speech.wav", tmp / "prev.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=0x406080:s=360x640", "-frames:v", "1", str(image))
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=200:duration=2.0:sample_rate=48000", "-ac", "1", str(audio))
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=360x640:r=25:d=1", "-pix_fmt", "yuv420p", str(prev))
    return image, audio, prev


async def _run(tmp: Path, *, continuation: bool, options: dict[str, Any] | None = None) -> tuple[Any, Any]:
    image, audio, prev = _media(tmp)
    ctx = LocalRunContext(tmp / "ctx", seed=11)
    if options:
        ctx.options = options  # type: ignore[attr-defined]
    adapter = PLUGIN.adapter().__class__(PLUGIN.manifest)
    backend = FakeInfiniteTalkBackend()
    adapter.use_backend(backend)
    await adapter.load(LoadContext(model_cache_dir=str(tmp / "models"), scratch_dir=str(tmp / "s"), app_env="test"))
    request = _request(
        None,
        keyframe=await ctx.put_file(image, "image"),
        audio=await ctx.put_file(audio, "audio"),
        continuation_frames=await ctx.put_file(prev, "video") if continuation else None,
        chunk_index=2 if continuation else 1,
    )
    result = await adapter.run("avatar.a2v", request, ctx)
    return result, (backend, ctx)


def test_the_adapter_conforms_the_clip_to_the_request(tmp_path: Path) -> None:
    result, (backend, ctx) = asyncio.run(_run(tmp_path, continuation=False))
    info = probe(asyncio.run(ctx.read_artifact(result.video)))
    assert (result.width, result.height, result.fps) == (720, 1280, 25.0)
    assert (info.width, info.height) == (720, 1280) and info.has_audio
    assert abs(info.duration_s - 2.0) < 0.1
    (params,) = backend.calls
    assert params.max_frames == 53 and params.prompt == "A person is talking. shot on a phone front camera"
    assert result.video.meta["params"]["seed"] == 11


def test_a_continuation_chunk_starts_from_the_previous_last_frame(tmp_path: Path) -> None:
    result, (backend, _) = asyncio.run(_run(tmp_path, continuation=True, options={"low_memory": True}))
    assert result.duration_s > 0
    (params,) = backend.calls
    assert params.num_persistent_param_in_dit == 0 and params.t5_cpu  # the OOM-retry settings (§25)
    assert any(p.name == "continuation.png" for p in (tmp_path / "ctx" / "scratch").rglob("*.png"))
