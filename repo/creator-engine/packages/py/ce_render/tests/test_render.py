"""ce_render: timeline resolution, ducking envelope, the mix with two-pass loudnorm, and the composite."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
from ce_core.text import tokenize
from ce_render.audio import SAMPLE_RATE, MixInput, assemble, decode, duck_envelope, mix
from ce_render.ffmpeg import measure_loudness, probe, run_ffmpeg
from ce_render.timeline import SegmentAudio, build_timeline, chunk_windows, shot_audio
from ce_render.video import CameraRamp, ComposeJob, Encode, Placement, Title, camera_post, compose, concat, make_proxy
from ce_testing.fixtures import example_spec

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def segment_audio(text: str, per_word: float = 0.3, lead: float = 0.05) -> SegmentAudio:
    n = len(tokenize(text))
    words = tuple((round(lead + i * per_word, 3), round(lead + i * per_word + per_word * 0.8, 3)) for i in range(n))
    return SegmentAudio(duration_s=round(lead + n * per_word + 0.1, 3), words=words)


def example_timeline():  # type: ignore[no-untyped-def]
    spec = example_spec()
    segments = {s.key: segment_audio(s.text) for s in spec.script.segments}
    return spec, segments, build_timeline(spec, segments)


def test_timeline_resolves_slots_cues_and_sfx() -> None:
    _, segments, tl = example_timeline()
    assert tl.segment_offsets["seg_1"] == 0.2
    assert tl.segment_offsets["seg_2"] == pytest.approx(0.2 + segments["seg_1"].duration_s + 0.25)
    assert [s.shot_key for s in tl.base] == ["sht_1"]
    assert tl.base[0].start_s == 0 and tl.base[0].end_s == tl.total_s
    overlay = tl.overlays[0]
    assert overlay.shot_key == "sht_2"
    assert overlay.start_s == tl.word_times["seg_2"][2][0] and overlay.end_s == tl.word_times["seg_2"][3][1]
    assert tl.sfx["sfx_1"] == overlay.start_s  # anchored at the shot with offset 0
    assert tl.music["mc_1"] == tl.scenes["scn_hook"] == (0.0, tl.total_s)
    emphasized = [w for w in tl.caption_words if w[3]]
    assert [w[0] for w in emphasized] == ["smarter", "not."]
    speech = tl.speech_intervals()
    assert speech[0][0] == pytest.approx(tl.word_times["seg_1"][0][0] - 0.1)


def test_shot_audio_and_chunk_windows() -> None:
    spec, _, tl = example_timeline()
    audio = shot_audio(tl, spec, "sht_1")
    assert audio.clip_start_s == pytest.approx(0.05)
    assert [p.segment_key for p in audio.pieces] == ["seg_1", "seg_2"]
    assert audio.pieces[1].at_s == pytest.approx(tl.segment_offsets["seg_2"] - audio.clip_start_s)
    assert len(audio.words) == 14
    windows = chunk_windows(audio, [8, 6])
    assert windows[0][0] == 0 and windows[-1][1] == audio.duration_s
    assert audio.words[7][1] < windows[0][1] < audio.words[8][0]


def test_duck_envelope_reaches_the_requested_depth_with_ramps() -> None:
    env = duck_envelope(SAMPLE_RATE * 3, [(1.0, 2.0)], duck_db=-18, attack_s=0.1, release_s=0.2)
    assert env[int(0.5 * SAMPLE_RATE)] == pytest.approx(1.0)
    assert 20 * np.log10(env[int(1.5 * SAMPLE_RATE)]) == pytest.approx(-18, abs=1e-3)
    mid_attack = 20 * np.log10(env[int(0.95 * SAMPLE_RATE)])
    assert -18 < mid_attack < 0
    assert env[int(2.5 * SAMPLE_RATE)] == pytest.approx(1.0)


async def _tone(path: Path, seconds: float, freq: int, volume: float = 0.5) -> Path:
    await run_ffmpeg(
        ["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-af", f"volume={volume}", str(path)]
    )
    return path


async def test_mix_ducks_music_and_hits_the_loudness_target(tmp_path: Path) -> None:
    seg = await _tone(tmp_path / "seg.wav", 1.0, 440)
    bed = await _tone(tmp_path / "bed.wav", 4.0, 220)
    hit = await _tone(tmp_path / "hit.wav", 0.2, 880)
    result = await mix(
        MixInput(
            dialogue=[(seg, 1.0)],
            music=[(bed, 0.0, 4.0, -18.0)],
            sfx=[(hit, 3.0, -6.0)],
            speech=[(1.0, 2.0)],
            total_s=4.0,
        ),
        tmp_path / "mix",
    )
    assert result.measured.integrated_lufs == pytest.approx(-14.0, abs=1.0)
    assert result.measured.true_peak_dbtp <= -0.5
    info = await probe(result.path)
    assert info.duration_s == pytest.approx(4.0, abs=0.05) and info.sample_rate == SAMPLE_RATE
    # music alone: ducked by 18 dB while speech is active
    music_only = await mix(
        MixInput(dialogue=[], music=[(bed, 0.0, 4.0, -18.0)], sfx=[], speech=[(1.0, 2.0)], total_s=4.0),
        tmp_path / "music",
    )
    samples = await decode(music_only.path)
    loud = np.sqrt(np.mean(samples[int(0.4 * SAMPLE_RATE) : int(0.8 * SAMPLE_RATE)] ** 2))
    quiet = np.sqrt(np.mean(samples[int(1.2 * SAMPLE_RATE) : int(1.8 * SAMPLE_RATE)] ** 2))
    assert 20 * np.log10(quiet / loud) == pytest.approx(-18, abs=1.0)


async def test_assemble_places_pieces_on_silence(tmp_path: Path) -> None:
    seg = await _tone(tmp_path / "seg.wav", 1.0, 440)
    out = await assemble([(seg, 0.25, 0.75, 0.5)], 2.0, tmp_path / "shot.wav")
    samples = (await decode(out, channels=1))[:, 0]
    assert samples.shape[0] == 2 * SAMPLE_RATE
    assert np.abs(samples[: int(0.45 * SAMPLE_RATE)]).max() == 0
    assert np.abs(samples[int(0.6 * SAMPLE_RATE) : int(0.9 * SAMPLE_RATE)]).max() > 0.03
    assert np.abs(samples[int(1.05 * SAMPLE_RATE) :]).max() == 0


async def _clip(path: Path, seconds: float, w: int, h: int, color: str, fps: int = 25) -> Path:
    await run_ffmpeg(
        ["-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={seconds}", "-vf", f"drawbox=c={color}:t=fill:w=40:h=40",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    )  # fmt: skip
    return path


async def test_camera_post_compose_and_proxy(tmp_path: Path) -> None:
    a = await _clip(tmp_path / "a.mp4", 2.0, 404, 720, "red")
    b = await _clip(tmp_path / "b.mp4", 1.5, 404, 720, "blue")
    joined = await concat([a, b], tmp_path / "ab.mp4")
    assert (await probe(joined)).duration_s == pytest.approx(3.5, abs=0.1)
    mezz = await camera_post(joined, tmp_path / "mezz.mp4", width=540, height=960, fps=30, punch_ins=[(1.0, 1.12)])
    info = await probe(mezz)
    assert (info.width, info.height, info.fps) == (540, 960, 30.0)
    broll = await _clip(tmp_path / "broll.mp4", 1.0, 640, 360, "green")
    audio = await _tone(tmp_path / "audio.wav", 4.0, 300, 0.2)
    ass = tmp_path / "c.ass"
    ass.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 540\nPlayResY: 960\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding\nStyle: Default,DejaVu Sans,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,10,10,40,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, "
        "MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,Hello there\n",
        encoding="utf-8",
    )
    job = ComposeJob(
        base=[Placement(mezz, 0.2, 0.0, 4.0)],
        overlays=[Placement(broll, 1.5, 1.5, 2.3)],
        titles=[Title("On: screen, 100%", 3.0, 3.8)],
        audio=audio,
        total_s=4.0,
        encode=Encode(width=540, height=960, fps=30, crf=23),
        captions_ass=ass,
        labels=["MOCK PROVENANCE — NOT FOR DISTRIBUTION"],
    )
    final = await compose(job, tmp_path / "final.mp4")
    info = await probe(final)
    assert (info.width, info.height, info.fps) == (540, 960, 30.0)
    assert info.duration_s == pytest.approx(4.0, abs=0.1)
    assert info.has_audio and info.audio_codec == "aac" and info.video_codec == "h264"
    proxy = await make_proxy(final, tmp_path / "proxy.mp4")
    assert (await probe(proxy)).height == 540
    loud = await measure_loudness(final)
    assert loud.integrated_lufs < 0


async def _frame_rgb(path: Path, at_s: float, width: int, height: int) -> np.ndarray:
    raw = path.with_suffix(".rgb")
    await run_ffmpeg(
        ["-ss", f"{at_s}", "-i", str(path), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)]
    )
    return np.frombuffer(raw.read_bytes(), dtype=np.uint8).reshape(height, width, 3)


async def test_a_brand_logo_is_overlaid_in_its_corner(tmp_path: Path) -> None:
    import dataclasses
    import hashlib

    from ce_render.video import Logo
    from ce_testing.placeholders import placeholder_png

    base = tmp_path / "base.mp4"  # a static grey picture: only the logo can change it
    await run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=gray:s=540x960:r=30:d=2", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(base)]
    )  # fmt: skip
    audio = await _tone(tmp_path / "audio.wav", 2.0, 300, 0.2)
    logo_png = tmp_path / "logo.png"
    logo_png.write_bytes(placeholder_png("brand-logo", 64, 32))
    r, g, b = hashlib.sha256(b"brand-logo").digest()[:3]
    plain = ComposeJob(
        base=[Placement(base, 0.0, 0.0, 2.0)],
        overlays=[],
        titles=[],
        audio=audio,
        total_s=2.0,
        encode=Encode(width=540, height=960, fps=30, crf=18),
    )
    branded = dataclasses.replace(
        plain, logo=Logo(logo_png, corner="top_right", width_ratio=0.2, margin_ratio=0.05, opacity=1.0)
    )
    without = await _frame_rgb(await compose(plain, tmp_path / "plain.mp4"), 1.0, 540, 960)
    frame = await _frame_rgb(await compose(branded, tmp_path / "branded.mp4"), 1.0, 540, 960)
    margin, lw, lh = round(0.05 * 540), 108, 54  # 20 % of 540; the 2:1 logo is 108 × 54
    inside = frame[margin + lh // 2, 540 - margin - lw // 2].astype(int)
    assert np.abs(inside - np.array([r, g, b])).max() < 40, (inside, (r, g, b))
    changed = np.abs(frame.astype(int) - without.astype(int)).max(axis=2) > 40
    rows, cols = np.nonzero(changed)
    assert rows.min() >= margin - 2 and rows.max() <= margin + lh + 2  # only the logo's box changed
    assert cols.min() >= 540 - margin - lw - 2 and cols.max() <= 540 - margin + 2


async def test_a_thumbnail_is_the_frame_with_its_text(tmp_path: Path) -> None:
    from ce_render.video import make_thumbnail

    clip = await _clip(tmp_path / "clip.mp4", 2.0, 404, 720, "red")
    plain = await make_thumbnail(clip, 1.0, tmp_path / "plain.png", width=320, height=180)
    texted = await make_thumbnail(clip, 1.0, tmp_path / "texted.png", width=320, height=180, text="Watch: 3 tips")
    for path in (plain, texted):
        info = await probe(path)
        assert (info.width, info.height) == (320, 180)
    a, b = await _frame_rgb(plain, 0, 320, 180), await _frame_rgb(texted, 0, 320, 180)
    changed = np.nonzero(np.abs(a.astype(int) - b.astype(int)).max(axis=2) > 40)[0]
    assert changed.size and changed.min() >= 180 * 0.6  # the text sits in the lower part


# ---------------------------------------------------------------------- camera moves (audit CAM-MOVES)
async def _pattern(path: Path, lum: str, seconds: float = 2.0) -> Path:
    """A still synthetic picture (`lum` is a geq luma expression) as an H.264 clip."""
    await run_ffmpeg(
        ["-f", "lavfi", "-i", f"nullsrc=s=640x1136:r=25:d={seconds},geq=lum='{lum}':cb=128:cr=128",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)]
    )  # fmt: skip
    return path


async def _luma(path: Path, at_s: float) -> float:
    return float((await _frame_rgb(path, at_s, 540, 960)).mean())


@pytest.mark.parametrize(
    ("move", "lum", "brighter_at_end"),
    [
        ("push_in", "255*hypot(X-W/2,Y-H/2)/hypot(W/2,H/2)", False),  # closer to the dark centre
        ("pull_out", "255*hypot(X-W/2,Y-H/2)/hypot(W/2,H/2)", True),
        ("pan", "255*X/W", True),  # turns from the dark left to the bright right
        ("tilt", "255*Y/H", True),
        ("whip", "255*X/W", True),
    ],
)
async def test_planned_camera_moves_change_the_picture(
    tmp_path: Path, move: str, lum: str, brighter_at_end: bool
) -> None:
    """Every `camera.move_type` the Director or an edit may plan is rendered: before the audit only
    punch-ins and handheld drift were, and push/pull/pan/tilt/whip left the shot unchanged."""
    src = await _pattern(tmp_path / "src.mp4", lum)
    still = await camera_post(src, tmp_path / "still.mp4", width=540, height=960, fps=25, duration_s=2.0)
    moved = await camera_post(
        src,
        tmp_path / "moved.mp4",
        width=540,
        height=960,
        fps=25,
        duration_s=2.0,
        ramps=[CameraRamp(move, 0.4, 1.9, 1.3 if move in ("push_in", "pull_out") else None)],
    )
    assert abs(await _luma(still, 0.2) - await _luma(still, 1.95)) < 1.0  # no move: the frame holds
    delta = await _luma(moved, 1.95) - await _luma(moved, 0.2)
    assert (delta > 3.0) if brighter_at_end else (delta < -3.0), delta
    if move == "whip":  # fast: done a quarter second after it starts
        assert abs(await _luma(moved, 0.8) - await _luma(moved, 1.95)) < 1.0


async def test_a_static_hold_stops_the_handheld_motion(tmp_path: Path) -> None:
    from ce_camera.motion import motion_for
    from ce_testing.build import config_bundle

    src = await _pattern(tmp_path / "src.mp4", "255*X/W", seconds=3.0)
    motion = motion_for(config_bundle().camera_profiles["pov"], seed=7)
    kwargs = {"width": 540, "height": 960, "fps": 25, "motion": motion, "duration_s": 3.0}
    shaky = await camera_post(src, tmp_path / "shaky.mp4", **kwargs)  # type: ignore[arg-type]
    held = await camera_post(src, tmp_path / "held.mp4", ramps=[CameraRamp("static_hold", 1.0, 3.0)], **kwargs)  # type: ignore[arg-type]
    after = [await _luma(held, t) for t in (1.6, 2.2, 2.8)]
    assert max(after) - min(after) < 0.3  # still after the hold
    moving = [await _luma(shaky, t) for t in (1.6, 2.2, 2.8)]
    assert max(moving) - min(moving) > max(after) - min(after)
