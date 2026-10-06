"""Render golden tests (§37, Phase 7 DoD): a full CPU render from synthetic shots — camera and
realism post, world acoustics, ducked music with room tone, two-pass loudnorm, captions through the
bundled font chain — encoded for three aspects, then checked like a delivery:

- ffprobe: streams, resolution, fps, duration;
- loudness −14 ± 1 LUFS and true peak ≤ −1 dBTP (`ebur128=peak=true`);
- captions present (PP-OCR on frames finds the caption words);
- aspect variants (9:16, 16:9 with the blurred-fill layout, 1:1);
- Arabic captions shaped and right-to-left (glyphs joined; the first letter on the right);
- the C2PA signature and hashes valid with the untrusted dev root (and a tampered copy failing).

Everything runs on CPU without model downloads (PP-OCR's models ship in its wheel)."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import numpy as np
import pytest
from ce_camera import motion_for
from ce_config.loader import load_config
from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import discover
from ce_policy.c2pa_verify import classify_c2pa
from ce_realism import ambient_bed, process_dialogue, realism_plan
from ce_render.audio import SAMPLE_RATE, MixInput, decode, encode_wav, mix
from ce_render.ffmpeg import measure_loudness, probe, run_ffmpeg
from ce_render.fonts import pick_font
from ce_render.video import ComposeJob, Encode, Placement, camera_post, compose, realism_post
from PIL import Image
from scipy import ndimage

pytestmark = [pytest.mark.golden, pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")]

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
REGISTRY = discover(app_env="test", include_mocks=True)
STYLE = BUNDLE.caption_styles["bold_pop_highlight"].model_dump(mode="json")
CHAIN = [STYLE["font_family"], *STYLE["font_fallbacks"]]
WORDS = ["Most", "people", "think", "productivity", "means", "more", "work."]


async def _adapter(plugin_id: str, tmp: Path):  # type: ignore[no-untyped-def]
    adapter = REGISTRY.get(plugin_id).adapter()
    await adapter.load(LoadContext(model_cache_dir=str(tmp / "models"), scratch_dir=str(tmp / "load"), app_env="test"))
    return adapter


async def _speech(ctx: LocalRunContext, tmp: Path) -> tuple[Path, list[tuple[float, float]]]:
    """Mock TTS (tone bursts per word with exact word timings), then the world acoustics chain."""
    voice = await _adapter("mock_voice", tmp)
    tts = await voice.run("voice.tts", m.TTSRequest(text=" ".join(WORDS), words=WORDS, language="en", wpm=150), ctx)
    dry = (await decode(await ctx.read_artifact(tts.audio), channels=1))[:, 0]
    room = BUNDLE.rooms["small_office"]
    import wave

    with wave.open(str(ROOT / "config" / "rooms" / room.impulse_response), "rb") as handle:
        ir = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    wet = process_dialogue(dry, SAMPLE_RATE, mic=BUNDLE.mic_profiles["phone_front_mic"], ir=ir, wet_mix=room.wet_mix)
    path = await encode_wav(wet, tmp / "dialogue.wav")
    return path, [(w.start_s, w.end_s) for w in tts.word_timings or []]


async def _shots(tmp: Path) -> list[Path]:
    """Two synthetic 9:16 shots through camera post (motion) and realism post (the profile's look)."""
    profile = BUNDLE.camera_profiles["phone_front_selfie"]
    out = []
    for i, colour in enumerate(("0x3a6ea5", "0xa5603a")):
        src = tmp / f"take_{i}.mp4"
        pattern = (
            f"testsrc2=s=720x1280:r=30:d=4,eq=saturation=0.6,drawbox=x=260:y=360:w=200:h=260:color={colour}:t=fill"
        )
        await run_ffmpeg(
            ["-f", "lavfi", "-i", pattern, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(src)]
        )
        mezz = await camera_post(
            src,
            tmp / f"cam_{i}.mp4",
            width=1080,
            height=1920,
            fps=30,
            motion=motion_for(profile, 11 + i),
            duration_s=4.0,
        )
        out.append(await realism_post(mezz, tmp / f"real_{i}.mp4", realism_plan(profile, ROOT / "config", 11 + i)))
    return out


async def _captions(
    ctx: LocalRunContext,
    tmp: Path,
    words: list[str],
    times: list[tuple[float, float]],
    *,
    width: int,
    height: int,
    language: str,
    rtl: bool,
    size_px: int | None = None,
) -> Path:
    builder = await _adapter("captions.ass_renderer", tmp)
    style = {
        **STYLE,
        "font_family": pick_font(CHAIN, " ".join(words)),
        **({"font_size_px": size_px} if size_px else {}),
    }
    request = m.CaptionBuildRequest(
        words=[m.CaptionWord(text=w, start_s=a, end_s=b) for w, (a, b) in zip(words, times, strict=True)],
        language=language,
        rtl=rtl,
        style=style,
        safe_zone={"top": 0.12, "bottom": 0.22, "left": 0.06, "right": 0.14},
        width=width,
        height=height,
        max_words_per_line=3,
        highlight="active_word",
    )
    result = await builder.run("captions.build", request, ctx)
    return await ctx.read_artifact(result.ass)


@pytest.fixture(scope="module")
def golden(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    tmp = tmp_path_factory.mktemp("golden")
    return asyncio.run(_build(tmp))


async def _build(tmp: Path) -> dict[str, object]:
    ctx = LocalRunContext(tmp / "ctx", seed=7)
    dialogue, word_times = await _speech(ctx, tmp)
    offset = 0.3
    words_on_timeline = [(a + offset, b + offset) for a, b in word_times]
    total = words_on_timeline[-1][1] + 0.8
    music = await _adapter("mock_audio", tmp)
    bed = await music.run("audio.music", m.MusicRequest(description="light bed", duration_s=total, bpm=100), ctx)
    room_tone = await encode_wav(
        ambient_bed(["room_tone_light_hvac"], total, SAMPLE_RATE, noise_floor_db=-62, seed=3), tmp / "bed.wav"
    )
    mixed = await mix(
        MixInput(
            dialogue=[(dialogue, offset)],
            music=[(await ctx.read_artifact(bed.audio), 0.0, total, -18.0)],
            sfx=[],
            speech=[(a - 0.1, b + 0.1) for a, b in words_on_timeline],
            total_s=total,
            beds=[(room_tone, 0.0, total)],
            true_peak_dbtp=-1.0 - BUNDLE.app.render.true_peak_codec_headroom_db,
        ),
        tmp / "mix",
    )
    shots = await _shots(tmp)
    half = total / 2
    renders: dict[str, Path] = {}
    for name, (w, h) in {"9x16": (1080, 1920), "16x9": (1920, 1080), "1x1": (1080, 1080)}.items():
        ass = await _captions(ctx, tmp / name, WORDS, words_on_timeline, width=w, height=h, language="en", rtl=False)
        layout = "blurred_fill" if name == "16x9" else "crop"
        job = ComposeJob(
            base=[
                Placement(shots[0], 0.0, 0.0, half, (0.5, 0.4), layout),
                Placement(shots[1], half, half, total, (0.5, 0.4), layout),
            ],
            overlays=[],
            titles=[],
            audio=mixed.path,
            total_s=total,
            encode=Encode(width=w, height=h, fps=30.0, crf=23, preset="ultrafast"),
            captions_ass=ass,
            labels=[],
        )
        renders[name] = await compose(job, tmp / f"final_{name}.mp4")
    return {"tmp": tmp, "ctx": ctx, "renders": renders, "mix": mixed, "times": words_on_timeline, "total": total}


def test_streams_resolution_fps_duration(golden: dict[str, object]) -> None:
    renders: dict[str, Path] = golden["renders"]  # type: ignore[assignment]
    expected = {"9x16": (1080, 1920), "16x9": (1920, 1080), "1x1": (1080, 1080)}
    for name, path in renders.items():
        info = asyncio.run(probe(path))
        assert (info.width, info.height) == expected[name]
        assert info.fps == 30.0 and info.video_codec == "h264" and info.audio_codec == "aac"
        assert info.duration_s == pytest.approx(float(golden["total"]), abs=0.15)  # type: ignore[arg-type]


def test_loudness_and_true_peak(golden: dict[str, object]) -> None:
    for path in golden["renders"].values():  # type: ignore[attr-defined]
        loud = asyncio.run(measure_loudness(path))
        assert loud.integrated_lufs == pytest.approx(-14.0, abs=1.0)
        assert loud.true_peak_dbtp <= -1.0 + 0.05  # AAC re-encode headroom is inside the measurement noise


def _frame(path: Path, t: float, out: Path) -> np.ndarray:
    asyncio.run(run_ffmpeg(["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1", str(out)]))
    return np.asarray(Image.open(out).convert("RGB"))


def test_captions_present_via_ocr(golden: dict[str, object]) -> None:
    """PP-OCRv6 (real, CPU) reads the burned captions back; its weights come from
    `make fetch-cpu-assets` (skip without them, a failure with CE_REQUIRE_CPU_ASSETS=1)."""
    import os

    from ce_contracts.manifest import missing_assets

    tmp: Path = golden["tmp"]  # type: ignore[assignment]
    times: list[tuple[float, float]] = golden["times"]  # type: ignore[assignment]
    cache = ROOT / ".cache" / "models"
    missing = missing_assets(REGISTRY.get("ppocr").manifest, str(cache))
    if missing:
        if os.environ.get("CE_REQUIRE_CPU_ASSETS"):
            pytest.fail(f"PP-OCR assets missing: {missing}")
        pytest.skip(f"PP-OCR assets missing (make fetch-cpu-assets): {missing}")
    ocr = REGISTRY.get("ppocr").adapter()
    asyncio.run(ocr.load(LoadContext(model_cache_dir=str(cache), scratch_dir=str(tmp / "ocr-load"), app_env="test")))
    seen: set[str] = set()
    for name, path in golden["renders"].items():  # type: ignore[attr-defined]
        for index in (0, 4):
            rgb = _frame(path, (times[index][0] + times[index][1]) / 2, tmp / f"ocr_{name}_{index}.png")
            text = " ".join(b.text for b in ocr.recognize(rgb)).lower()
            seen.update(w.strip(".").lower() for w in WORDS if w.strip(".").lower() in text)
        assert seen, f"no caption words found by OCR in {name}"
    assert {"most", "people", "productivity"} & seen


def test_aspect_variant_uses_the_blurred_fill_layout(golden: dict[str, object]) -> None:
    tmp: Path = golden["tmp"]  # type: ignore[assignment]
    rgb = _frame(golden["renders"]["16x9"], 0.5, tmp / "wide.png")  # type: ignore[index]
    gray = rgb.astype(float).mean(axis=2)
    edges = np.abs(np.diff(gray, axis=1))
    side, centre = edges[:, :300].mean(), edges[:, 760:1160].mean()
    assert gray[:, :300].mean() > 8.0  # a picture on the sides, not black bars
    assert side < 0.35 * centre  # …and a blurred one: the sharp foreground sits in the middle


def _components(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (x0, y0, x1, y1) of ink blobs larger than dots."""
    labels, _n = ndimage.label(gray > 128)
    boxes = []
    for sl in ndimage.find_objects(labels):
        if sl is None:
            continue
        ys, xs = sl
        if (ys.stop - ys.start) * (xs.stop - xs.start) > 150:
            boxes.append((xs.start, ys.start, xs.stop, ys.stop))
    return boxes


def _render_text(text: str, out: Path, *, rtl: bool, tmp: Path) -> np.ndarray:
    ctx = LocalRunContext(tmp / f"ctx_{out.stem}")
    words = text.split()
    times = [(0.0, 2.0)] * len(words)
    ass = asyncio.run(
        _captions(ctx, tmp / out.stem, words, times, width=1920, height=1080, language="ar", rtl=rtl, size_px=220)
    )
    burn = f"ass={ass}:fontsdir={ROOT / 'assets' / 'fonts'}"
    asyncio.run(
        run_ffmpeg(["-f", "lavfi", "-i", "color=c=black:s=1920x1080:d=1", "-vf", burn, "-frames:v", "1", str(out)])
    )
    return np.asarray(Image.open(out).convert("L"))


def test_arabic_captions_are_shaped_and_right_to_left(tmp_path: Path) -> None:
    joined = _components(_render_text("سلام", tmp_path / "joined.png", rtl=True, tmp=tmp_path))
    separate = _components(_render_text("س ل ا م", tmp_path / "separate.png", rtl=True, tmp=tmp_path))
    assert 1 <= len(joined) < len(separate), (joined, separate)  # contextual forms join the letters
    order = _components(_render_text("ا ب", tmp_path / "order.png", rtl=True, tmp=tmp_path))
    rightmost = max(order, key=lambda b: b[2])
    width, height = rightmost[2] - rightmost[0], rightmost[3] - rightmost[1]
    assert height > 2 * width, order  # alef (tall and narrow), the first letter, sits on the right


def test_c2pa_signature_and_hashes_valid_with_untrusted_dev_root(golden: dict[str, object]) -> None:
    tmp: Path = golden["tmp"]  # type: ignore[assignment]
    ctx: LocalRunContext = golden["ctx"]  # type: ignore[assignment]

    async def run() -> tuple[m.VerifyResult, m.VerifyResult]:
        signer = await _adapter("c2pa_signer", tmp)
        media = await ctx.put_file(golden["renders"]["9x16"], "video")  # type: ignore[index]
        media = media.model_copy(update={"mime": "video/mp4"})
        signed = await signer.run(
            "provenance.sign",
            m.SignRequest(
                media=media,
                manifest={
                    "digital_source_type": "trainedAlgorithmicMedia",
                    "ingredients": ["mock_avatar_global@1"],
                    "consent_ids": [],
                },
            ),
            ctx,
        )
        assert signed.mode == "real"
        good = await signer.run("provenance.verify", m.VerifyRequest(media=signed.media, layer="c2pa"), ctx)
        path = await ctx.read_artifact(signed.media)
        tampered = tmp / "tampered.mp4"
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF  # one flipped byte in the media data
        tampered.write_bytes(bytes(data))
        bad_ref = (await ctx.put_file(tampered, "video")).model_copy(update={"mime": "video/mp4"})
        bad = await signer.run("provenance.verify", m.VerifyRequest(media=bad_ref, layer="c2pa"), ctx)
        return good, bad

    good, bad = asyncio.run(run())
    verdict = classify_c2pa(good.present, good.success, good.failures)
    assert verdict.signature_valid and verdict.hashes_valid and not verdict.trusted
    assert verdict.ok_untrusted_root, good
    actions = good.detail["assertions"]["c2pa.actions.v2"]["actions"][0]
    assert actions["digitalSourceType"].endswith("trainedAlgorithmicMedia")
    assert not classify_c2pa(bad.present, bad.success, bad.failures).ok_untrusted_root
