"""ASS, SRT and VTT captions from word timings (§27).

- Lines hold at most `max_words_per_line` words and break early at sentence punctuation or at a
  gap longer than `LINE_GAP_S`.
- `active_word`: one event per word, the active word coloured with the style's highlight colour;
  `phrase`: one event per line; `none`: plain lines. Right-to-left tracks fall back to the style's
  `rtl_highlight` (line level), because per-word colour runs break bidi shaping.
- Placement keeps text inside the platform safe zone: the bottom margin is the zone's bottom
  fraction of the frame height, side margins follow the zone.
- Emphasis words (caption_emphasis) are scaled up slightly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ce_contracts.common import RunContext
from ce_contracts.interfaces import CaptionEngine
from ce_contracts.models import CaptionBuildRequest, CaptionResult, CaptionWord

__all__ = ["AssCaptionBuilder", "build_ass", "build_srt", "build_vtt", "group_lines"]

LINE_GAP_S = 0.6
REFERENCE_HEIGHT = 1920
_BREAK = (".", "!", "?", "…", ":", ";", "؟", "。")


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    rgb = hex_rgb.lstrip("#")
    r, g, b = rgb[0:2], rgb[2:4], rgb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(seconds: float, sep: str = ",") -> str:
    ms = max(0, round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _clean(text: str) -> str:
    """Text is data: braces and backslashes would become ASS override tags, so they are removed."""
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_lines(words: list[CaptionWord], max_words: int) -> list[list[CaptionWord]]:
    lines: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for word in words:
        if current and (len(current) >= max_words or word.start_s - current[-1].end_s > LINE_GAP_S):
            lines.append(current)
            current = []
        current.append(word)
        if word.text.rstrip("\"'»”").endswith(_BREAK):
            lines.append(current)
            current = []
    if current:
        lines.append(current)
    return lines


def _line_text(line: list[CaptionWord], *, active: int | None, highlight: str, primary: str) -> str:
    parts = []
    for index, word in enumerate(line):
        text = _clean(word.text)
        if word.emphasis:
            text = "{\\fscx112\\fscy112}" + text + "{\\fscx100\\fscy100}"
        if active is not None and index == active:
            text = "{\\c" + highlight + "&}" + text + "{\\c" + primary + "&}"
        parts.append(text)
    return " ".join(parts)


def build_ass(request: CaptionBuildRequest) -> str:
    style = request.style
    scale = request.height / REFERENCE_HEIGHT
    size = max(10, round(float(style.get("font_size_px", 64)) * scale))
    outline = max(0, round(float(style.get("stroke_width_px", 4)) * scale))
    primary = _ass_color(style.get("primary_color", "#FFFFFF"))
    highlight = _ass_color(style.get("highlight_color", "#FFD400"))
    stroke = _ass_color(style.get("stroke_color", "#000000"))
    zone = request.safe_zone or {"top": 0.08, "bottom": 0.12, "left": 0.06, "right": 0.06}
    margin_v = round(request.height * float(zone.get("bottom", 0.12)))
    margin_l = round(request.width * float(zone.get("left", 0.06)))
    margin_r = round(request.width * float(zone.get("right", 0.06)))
    alignment = {"bottom": 2, "center": 5, "top": 8}.get(request.placement, 2)
    if alignment == 8:
        margin_v = round(request.height * float(zone.get("top", 0.08)))
    font = str(style.get("font_family", "DejaVu Sans"))
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {request.width}",
        f"PlayResY: {request.height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Caption,{font},{size},{primary},{highlight},{stroke},&H80000000,-1,0,0,0,100,100,0,0,1,{outline},0,"
        f"{alignment},{margin_l},{margin_r},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    mode = request.highlight
    if request.rtl and mode == "active_word":
        mode = "phrase" if style.get("rtl_highlight", "line") == "line" else "none"
    events: list[str] = []
    for line in group_lines(request.words, request.max_words_per_line):
        start, end = line[0].start_s, line[-1].end_s
        if mode == "active_word":
            for index, word in enumerate(line):
                w_start = word.start_s if index else start
                w_end = line[index + 1].start_s if index + 1 < len(line) else end
                text = _line_text(line, active=index, highlight=highlight, primary=primary)
                events.append(
                    f"Dialogue: 0,{_ass_time(w_start)},{_ass_time(max(w_end, w_start + 0.01))},Caption,,0,0,0,,{text}"
                )
        else:
            text = _line_text(line, active=None, highlight=highlight, primary=primary)
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Caption,,0,0,0,,{text}")
    return "\n".join(header + events) + "\n"


def _plain_lines(request: CaptionBuildRequest) -> list[tuple[float, float, str]]:
    return [
        (line[0].start_s, line[-1].end_s, " ".join(_clean(w.text) for w in line))
        for line in group_lines(request.words, request.max_words_per_line)
    ]


def build_srt(request: CaptionBuildRequest) -> str:
    blocks = [
        f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n"
        for i, (a, b, text) in enumerate(_plain_lines(request), start=1)
    ]
    return "\n".join(blocks)


def build_vtt(request: CaptionBuildRequest) -> str:
    blocks = [f"{_srt_time(a, '.')} --> {_srt_time(b, '.')}\n{text}\n" for a, b, text in _plain_lines(request)]
    return "WEBVTT\n\n" + "\n".join(blocks)


class AssCaptionBuilder(CaptionEngine):
    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)

    async def run_captions_build(self, request: CaptionBuildRequest, ctx: RunContext) -> CaptionResult:
        work = Path(ctx.scratch_dir) / "captions"
        work.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for ext, builder, mime in (
            ("ass", build_ass, "text/x-ssa"),
            ("srt", build_srt, "application/x-subrip"),
            ("vtt", build_vtt, "text/vtt"),
        ):
            path = work / f"captions.{ext}"
            path.write_text(builder(request), encoding="utf-8")
            outputs[ext] = await ctx.write_artifact(
                path, "captions", {"format": ext, "language": request.language, "rtl": request.rtl}, role=ext, mime=mime
            )
        return CaptionResult(ass=outputs["ass"], srt=outputs["srt"], vtt=outputs["vtt"])
