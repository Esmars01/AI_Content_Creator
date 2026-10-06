"""Phase 0: FFmpeg/libass can shape right-to-left text (HarfBuzz + FriBidi), checked by rendering it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed (needed from Phase 2)")

ARABIC_ASS = """[Script Info]
ScriptType: v4.00+
PlayResX: 320
PlayResY: 180

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, \
Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, \
MarginV, Encoding
Style: Default,DejaVu Sans,28,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,مرحبا بالعالم
"""


def test_ffmpeg_build_flags() -> None:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True, check=True).stdout
    for flag in ("--enable-libass", "--enable-libharfbuzz", "--enable-libfribidi", "--enable-libfreetype"):
        assert flag in out, f"ffmpeg lacks {flag}"


def test_libass_uses_fribidi_and_harfbuzz_when_rendering_arabic(tmp_path: Path) -> None:
    subs = tmp_path / "rtl.ass"
    subs.write_text(ARABIC_ASS, encoding="utf-8")
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-v",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:d=1",
            "-vf",
            f"ass={subs.name}",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    log = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, log[-2000:]
    shaper = next((line for line in log.splitlines() if "Shaper:" in line), "")
    assert "FriBidi" in shaper and "HarfBuzz" in shaper, f"libass shaper line: {shaper!r}"
    frame = result.stdout
    assert len(frame) == 320 * 180
    lit = sum(1 for px in frame if px > 128)
    assert lit > 200, "the Arabic caption did not render any visible glyphs"
