"""The delivered file meets the true-peak target, not only the mix (§27, Phase 14): AAC encoding
can overshoot a limited mix by several dB (a demo render measured −2.5 dBTP in the mix and +1.0 dBTP
after the 192 kbit/s AAC encode). `enforce_true_peak` re-encodes the audio from the mix at higher
bitrates, then attenuates as a last resort."""

from __future__ import annotations

from pathlib import Path

from ce_render.ffmpeg import measure_loudness, probe, run_ffmpeg
from ce_render.video import enforce_true_peak

CEILING = -1.0


async def _clip(tmp: Path, amplitude: float, kbps: int) -> tuple[Path, Path]:
    """A 2 s clip whose audio is a 1 kHz square wave (AAC overshoots it strongly at low bitrates)."""
    mix = tmp / "mix.wav"
    await run_ffmpeg(
        ["-f", "lavfi", "-i", f"aevalsrc='{amplitude}*sgn(sin(2*PI*1000*t))':s=48000:d=2", "-ac", "2", str(mix)]
    )
    video = tmp / "final.mp4"
    await run_ffmpeg(
        ["-f", "lavfi", "-i", "color=c=gray:s=320x240:r=30:d=2", "-i", str(mix), "-c:v", "libx264",
         "-preset", "veryfast", "-c:a", "aac", "-b:a", f"{kbps}k", "-shortest", str(video)]
    )  # fmt: skip
    return video, mix


async def test_an_overshooting_encode_is_fixed_and_says_how(tmp_path: Path) -> None:
    video, mix = await _clip(tmp_path, 0.75, 96)
    encoded = (await measure_loudness(video)).true_peak_dbtp
    assert encoded > CEILING  # the encode overshoots although the mix (≈ −3.4 dBTP) is under the ceiling
    fix = await enforce_true_peak(
        video, mix, tmp_path / "fix", ceiling_dbtp=CEILING, bitrate_kbps=96, retry_bitrates_kbps=[192, 320]
    )
    assert fix.action in ("bitrate", "attenuated") and fix.true_peak_before == encoded
    measured = (await measure_loudness(fix.path)).true_peak_dbtp
    assert measured <= CEILING + 0.05 and abs(measured - fix.true_peak_after) < 0.01
    if fix.action == "attenuated":
        assert fix.attenuation_db > 0
    before, after = await probe(video), await probe(fix.path)
    assert abs(after.duration_s - before.duration_s) < 0.1 and (after.width, after.height) == (320, 240)


async def test_attenuation_is_the_last_resort(tmp_path: Path) -> None:
    video, mix = await _clip(tmp_path, 0.75, 96)
    fix = await enforce_true_peak(video, mix, tmp_path / "fix", ceiling_dbtp=CEILING, bitrate_kbps=96)
    assert fix.action == "attenuated" and fix.attenuation_db > 0 and fix.audio_bitrate_kbps == 96
    assert (await measure_loudness(fix.path)).true_peak_dbtp <= CEILING + 0.05


async def test_a_compliant_file_is_left_alone(tmp_path: Path) -> None:
    video, mix = await _clip(tmp_path, 0.2, 192)
    fix = await enforce_true_peak(video, mix, tmp_path / "fix", ceiling_dbtp=CEILING, bitrate_kbps=192)
    assert fix.action == "none" and fix.path == video
