"""Video assembly (§27): shot mezzanines, the base/overlay composite, burned captions and labels,
the final encode per render preset, and the 540p proxy.

Mezzanines (`camera_post`) are cut to the output frame (subject-aware reframing or a centred crop),
carry the shot's procedural camera motion and punch-ins and run at the preset fps; `realism_post`
applies the camera profile's look (`ce_camera`, `ce_realism`, Phase 7).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from ce_camera.motion import REFERENCE_HEIGHT, CameraMotion, FocusHunt
from ce_camera.reframe import CropPlan, piecewise_expression
from ce_realism.video import RealismPlan

from ce_render.ffmpeg import escape_filter_path, escape_text, font_file, measure_loudness, run_ffmpeg
from ce_render.fonts import fonts_dir

__all__ = [
    "ComposeJob",
    "Encode",
    "Logo",
    "Placement",
    "Title",
    "TruePeakFix",
    "camera_post",
    "compose",
    "concat",
    "enforce_true_peak",
    "make_proxy",
    "make_thumbnail",
    "mezzanine_args",
    "realism_post",
]

MEZZANINE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-an"]


def mezzanine_args() -> list[str]:
    return list(MEZZANINE)


@dataclass(frozen=True)
class Encode:
    """Delivery encode from a `render_presets` entry."""

    width: int
    height: int
    fps: float
    crf: int = 18
    max_bitrate_kbps: int | None = None
    audio_bitrate_kbps: int = 192
    preset: str = "veryfast"

    def video_args(self) -> list[str]:
        args = ["-c:v", "libx264", "-preset", self.preset, "-crf", str(self.crf), "-pix_fmt", "yuv420p"]
        if self.max_bitrate_kbps:
            args += ["-maxrate", f"{self.max_bitrate_kbps}k", "-bufsize", f"{2 * self.max_bitrate_kbps}k"]
        return [*args, "-r", f"{self.fps:g}", "-g", str(round(self.fps * 2))]

    def audio_args(self) -> list[str]:
        return ["-c:a", "aac", "-b:a", f"{self.audio_bitrate_kbps}k", "-ar", "48000", "-ac", "2"]


async def concat(paths: Sequence[Path], out: Path) -> Path:
    """Concatenates clips of one shape (chunks of a take) with a re-encode."""
    if len(paths) == 1:
        await run_ffmpeg(["-i", str(paths[0]), *MEZZANINE, str(out)])
        return out
    inputs: list[str] = []
    for path in paths:
        inputs += ["-i", str(path)]
    graph = "".join(f"[{i}:v]setpts=PTS-STARTPTS[v{i}];" for i in range(len(paths)))
    graph += "".join(f"[v{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=1:a=0[out]"
    await run_ffmpeg([*inputs, "-filter_complex", graph, "-map", "[out]", *MEZZANINE, str(out)])
    return out


def _motion_margin(motion: CameraMotion | None, width: int, height: int, duration_s: float) -> float:
    """Extra scale so camera shake and rotation never reveal the frame edge."""
    if motion is None or motion.still:
        return 1.0
    shift = motion.peak_px(height, duration_s)
    t = [i * duration_s / 256 for i in range(257)]
    angle = max(float(np.abs(motion.angle(np.asarray(t))).max(initial=0.0)), 0.0)
    rotation = math.cos(angle) + math.sin(angle) * max(width / height, height / width)
    return rotation * (1.0 + 2.2 * shift / min(width, height))


def _reframe_filter(crop: CropPlan, width: int, height: int) -> str:
    """The subject-aware pre-crop (or the blurred-fill layout) as a filter chain ending at `width`×`height`."""
    if crop.layout == "blurred_fill":
        return (
            f"split=2[bg][fg];[bg]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"boxblur=luma_radius=24:luma_power=2[bgb];[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2"
        )
    xs = piecewise_expression([(t, x) for t, x, _ in crop.path], default=(1 - crop.width) / 2)
    ys = piecewise_expression([(t, y) for t, _, y in crop.path], default=(1 - crop.height) / 2)
    return f"crop=w=trunc(iw*{crop.width:.4f}/2)*2:h=trunc(ih*{crop.height:.4f}/2)*2:x='iw*({xs})':y='ih*({ys})'"


async def camera_post(
    src: Path,
    out: Path,
    *,
    width: int,
    height: int,
    fps: float,
    punch_ins: Sequence[tuple[float, float]] = (),
    drift_px: float = 0.0,
    motion: CameraMotion | None = None,
    crop: CropPlan | None = None,
    focus: Sequence[FocusHunt] = (),
    exposure: str | None = None,
    blur_frames: int = 0,
    duration_s: float = 10.0,
) -> Path:
    """Post camera (§22, `ce_camera`): an optional subject-aware pre-crop (reframing, or the
    blurred-fill layout when the crop loses too much of the subject), scale to cover
    `width`×`height` with a margin for shake and rotation, seeded procedural motion (translation
    and rotation), punch-ins `(t_s, scale)` (a cut to the new scale at `t_s`), the editorial
    handheld drift, frame-rate conversion, motion blur by speed (`blur_frames` blended), autofocus
    hunts (brief blur windows) and exposure drift. Deterministic given its inputs."""
    margin = _motion_margin(motion, width, height, duration_s)
    if drift_px > 0:
        margin *= 1.0 + 2.5 * drift_px / min(width, height)
    zoom = f"{margin:.4f}"
    for t, scale in sorted(punch_ins):
        zoom = f"if(gte(t\\,{t:.3f})\\,{scale * margin:.4f}\\,{zoom})"
    x = f"(iw-{width})/2"
    y = f"(ih-{height})/2"
    k = height / REFERENCE_HEIGHT
    if motion is not None and not motion.still:
        x += "+" + motion.x.expression(f"{k:.4f}").replace(",", "\\,")
        y += "+" + motion.y.expression(f"{k:.4f}").replace(",", "\\,")
    if drift_px > 0:
        x += f"+{drift_px:.2f}*sin(2*PI*0.37*t)"
        y += f"+{drift_px:.2f}*cos(2*PI*0.29*t+0.7)"
    chain: list[str] = []
    if crop is not None and (crop.layout == "blurred_fill" or crop.path):
        chain.append(_reframe_filter(crop, width, height))
    chain += [
        f"scale=w='max({width}/iw\\,{height}/ih)*iw':h='max({width}/iw\\,{height}/ih)*ih':flags=lanczos",
        f"scale=w='trunc(iw*{zoom}/2)*2':h='trunc(ih*{zoom}/2)*2':eval=frame:flags=lanczos",
    ]
    if motion is not None and motion.angle.terms:
        chain.append(f"rotate=a='{motion.angle.expression()}':ow=iw:oh=ih:c=black")
    chain += [f"crop={width}:{height}:'{x}':'{y}'", "setsar=1", f"fps={fps:g}"]
    if blur_frames >= 2:
        weights = " ".join(["1"] * blur_frames)
        chain.append(f"tmix=frames={blur_frames}:weights='{weights}'")
    for hunt in focus:
        a, b = hunt.start_s, hunt.start_s + hunt.duration_s
        mid = (a + b) / 2
        chain.append(f"avgblur=sizeX=2:enable='between(t,{a:.3f},{b:.3f})'")
        chain.append(f"avgblur=sizeX=3:enable='between(t,{mid - 0.08:.3f},{mid + 0.08:.3f})'")
    if exposure:
        chain.append(f"eq=brightness='{exposure}':eval=frame")
    chain.append("format=yuv420p")
    await run_ffmpeg(["-i", str(src), "-vf", ",".join(chain), *MEZZANINE, str(out)])
    return out


async def realism_post(src: Path, out: Path, plan: RealismPlan) -> Path:
    """Realism post (§22, `ce_realism`): the plan's filters, then a platform-like encode at the
    profile's bitrate (its compression artifacts are part of the look)."""
    if not plan.filters:
        await run_ffmpeg(["-i", str(src), *MEZZANINE, str(out)])
        return out
    encode = MEZZANINE
    if plan.bitrate_kbps:
        kbps = plan.bitrate_kbps
        encode = ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{kbps}k", "-maxrate", f"{round(kbps * 1.5)}k",
                  "-bufsize", f"{kbps * 2}k", "-pix_fmt", "yuv420p", "-an"]  # fmt: skip
    await run_ffmpeg(["-i", str(src), "-vf", ",".join(plan.filters), *encode, str(out)])
    return out


@dataclass(frozen=True)
class Placement:
    """A clip on the timeline: clip t=0 is at `clip_start_s`; it shows during [start_s, end_s].
    For an output of another aspect, `focus` places the crop (fractions of the free space, from the
    subject) and `layout` may be `blurred_fill` when cropping would lose the subject (§22)."""

    path: Path
    clip_start_s: float
    start_s: float
    end_s: float
    focus: tuple[float, float] | None = None
    layout: str = "crop"
    bubble: tuple[str, float] | None = None  # screen shots: the webcam bubble's (corner, size), §27


BUBBLE_FACE_Y = 0.42  # the bubble frames the base track around the eye line


def _bubble_filters(label_in: str, label_out: str, *, w: int, h: int, size: float) -> tuple[str, int]:
    """The webcam bubble (§27): a square of the base track around the face, scaled to `size` of
    the frame's short side, masked to a circle with a thin light ring. Returns (filter, diameter)."""
    short = min(w, h)
    d = max(16, round(size * short / 2) * 2)
    c = min(int(short * 0.7) // 2 * 2, w, h)
    cx, cy = (w - c) // 2, int(min(max(BUBBLE_FACE_Y * h - c / 2, 0), h - c))
    ring = max(2.0, d * 0.025)
    r = d / 2 - 1
    inside = f"lte(hypot(X-W/2\\,Y-H/2)\\,{r:.2f})"
    on_ring = f"between(hypot(X-W/2\\,Y-H/2)\\,{r - ring:.2f}\\,{r:.2f})"
    chain = (
        f"[{label_in}]crop={c}:{c}:{cx}:{cy},scale={d}:{d},format=yuva444p,"
        f"geq=lum='if({on_ring}\\,235\\,lum(X\\,Y))':cb='if({on_ring}\\,128\\,cb(X\\,Y))'"
        f":cr='if({on_ring}\\,128\\,cr(X\\,Y))':a='255*{inside}'[{label_out}]"
    )
    return chain, d


@dataclass(frozen=True)
class Title:
    text: str
    start_s: float
    end_s: float


@dataclass(frozen=True)
class Logo:
    """A brand logo burned over the picture (under the labels): scaled to `width_ratio` of the
    frame width, `margin_ratio` of the short side from the corner."""

    path: Path
    corner: str = "top_right"
    width_ratio: float = 0.14
    margin_ratio: float = 0.04
    opacity: float = 0.9


@dataclass
class ComposeJob:
    base: list[Placement]
    overlays: list[Placement]
    titles: list[Title]
    audio: Path
    total_s: float
    encode: Encode
    captions_ass: Path | None = None
    labels: list[str] = field(default_factory=list)  # burned labels, top of frame
    logo: Logo | None = None


def _drawtext(text: str, *, size: int, x: str, y: str, enable: str | None = None, color: str = "white") -> str:
    parts = [
        f"fontfile='{escape_filter_path(font_file())}'",
        f"text='{escape_text(text)}'",
        f"fontsize={size}",
        f"fontcolor={color}",
        "box=1",
        "boxcolor=black@0.65",
        f"boxborderw={max(6, size // 3)}",
        f"x={x}",
        f"y={y}",
    ]
    if enable:
        parts.append(f"enable='{enable}'")
    return "drawtext=" + ":".join(parts)


async def compose(job: ComposeJob, out: Path) -> Path:
    """Base track cut back to back, overlays composited, titles, captions and labels burned, the
    mixed audio muxed, encoded per preset (§27)."""
    enc = job.encode
    w, h, fps = enc.width, enc.height, enc.fps
    inputs: list[str] = []
    graph: list[str] = []
    tail = f"setsar=1,fps={fps:g},format=yuv420p"
    for index, clip in enumerate([*job.base, *job.overlays]):
        inputs += ["-i", str(clip.path)]
        duration = max(0.0, clip.end_s - clip.start_s)
        pre = max(0.0, clip.clip_start_s - clip.start_s)
        skip = max(0.0, clip.start_s - clip.clip_start_s)
        label = f"b{index}" if index < len(job.base) else f"o{index - len(job.base)}"
        shift = "" if index < len(job.base) else f"+{clip.start_s:.6f}/TB"
        if clip.layout == "blurred_fill":
            head = (
                f"[{index}:v]split=2[bg{index}][fg{index}];"
                f"[bg{index}]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                f"boxblur=luma_radius=24:luma_power=2[bgb{index}];"
                f"[fg{index}]scale={w}:{h}:force_original_aspect_ratio=decrease[fgs{index}];"
                f"[bgb{index}][fgs{index}]overlay=(W-w)/2:(H-h)/2,{tail}"
            )
        else:
            fx, fy = clip.focus or (0.5, 0.5)
            head = (
                f"[{index}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h}:(iw-ow)*{fx:.4f}:(ih-oh)*{fy:.4f},{tail}"
            )
        graph.append(
            f"{head},tpad=start_duration={pre:.6f}:start_mode=clone"
            f":stop_duration={duration + 1:.6f}:stop_mode=clone,"
            f"trim=start={skip:.6f}:duration={duration:.6f},setpts=PTS-STARTPTS{shift}[{label}]"
        )
    n_base = len(job.base)
    if n_base == 0:
        graph.append(f"color=c=black:s={w}x{h}:r={fps:g}:d={job.total_s:.6f},format=yuv420p[base]")
    else:
        graph.append("".join(f"[b{i}]" for i in range(n_base)) + f"concat=n={n_base}:v=1:a=0[base]")
    current = "base"
    bubbles = [j for j, clip in enumerate(job.overlays) if clip.bubble is not None]
    if bubbles:  # the bubble shows the base track (the creator) over the screen overlay
        graph.append(f"[base]split={len(bubbles) + 1}[base0]" + "".join(f"[bb{j}]" for j in bubbles))
        current = "base0"
    for j, clip in enumerate(job.overlays):
        enable = f"between(t,{clip.start_s:.3f},{clip.end_s:.3f})"
        graph.append(f"[{current}][o{j}]overlay=eof_action=pass:enable='{enable}'[v{j}]")
        current = f"v{j}"
        if clip.bubble is not None:
            corner, size = clip.bubble
            chain, d = _bubble_filters(f"bb{j}", f"bub{j}", w=w, h=h, size=size)
            margin = round(0.04 * min(w, h))
            x = margin if "left" in corner else w - d - margin
            y = round(0.08 * h) if "top" in corner else h - d - round(0.08 * h)
            graph.append(chain)
            graph.append(f"[{current}][bub{j}]overlay={x}:{y}:eof_action=pass:enable='{enable}'[vb{j}]")
            current = f"vb{j}"
    audio_index = n_base + len(job.overlays)
    if job.logo is not None:
        logo = job.logo
        inputs += ["-i", str(logo.path)]
        lw = max(2, round(w * logo.width_ratio / 2) * 2)
        margin = round(logo.margin_ratio * min(w, h))
        lx = str(margin) if "left" in logo.corner else f"W-w-{margin}"
        ly = str(margin) if "top" in logo.corner else f"H-h-{margin}"
        graph.append(
            f"[{audio_index}:v]scale={lw}:-2,format=rgba,colorchannelmixer=aa={logo.opacity:.3f}[logo];"
            f"[{current}][logo]overlay={lx}:{ly}:eof_action=repeat[vlogo]"
        )
        current = "vlogo"
        audio_index += 1
    post: list[str] = []
    for title in job.titles:
        post.append(
            _drawtext(
                title.text,
                size=round(h * 0.045),
                x="(w-text_w)/2",
                y="h*0.38",
                enable=f"between(t,{title.start_s:.3f},{title.end_s:.3f})",
            )
        )
    if job.captions_ass is not None:
        fonts = escape_filter_path(fonts_dir())  # the bundled Noto chain (§27)
        post.append(f"ass='{escape_filter_path(job.captions_ass)}':fontsdir='{fonts}'")
    for k, text in enumerate(job.labels):
        size = round(h * 0.022)
        post.append(_drawtext(text, size=size, x="(w-text_w)/2", y=f"{round(h * 0.11) + k * round(size * 2.2)}"))
    post.append("format=yuv420p")
    graph.append(f"[{current}]" + ",".join(post) + "[vout]")
    inputs += ["-i", str(job.audio)]
    await run_ffmpeg(
        [
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[vout]",
            "-map",
            f"{audio_index}:a",
            *enc.video_args(),
            *enc.audio_args(),
            "-t",
            f"{job.total_s:.6f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


async def make_proxy(src: Path, out: Path, *, height: int = 540) -> Path:
    """The fast 540p preview (§27)."""
    await run_ffmpeg(
        [
            "-i",
            str(src),
            "-vf",
            f"scale=-2:{height}:flags=bilinear",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


async def make_thumbnail(
    video: Path, at_s: float, out: Path, *, width: int, height: int, text: str = "", text_size_ratio: float = 0.06
) -> Path:
    """A packaging thumbnail (Phase 12): the frame at `at_s`, cover-scaled to `width` × `height`,
    with an optional one-line overlay text in a box at the lower third. PNG."""
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        "setsar=1",
    ]
    if text.strip():
        filters.append(
            _drawtext(text.strip(), size=max(12, round(height * text_size_ratio)), x="(w-text_w)/2", y="h*0.68")
        )
    await run_ffmpeg(
        ["-ss", f"{max(0.0, at_s):.3f}", "-i", str(video), "-frames:v", "1", "-vf", ",".join(filters), str(out)]
    )
    return out


@dataclass(frozen=True)
class TruePeakFix:
    """What `enforce_true_peak` delivered: the file, the audio bitrate it ended with, the true peak
    measured before and after, and what it did (`none`, `bitrate`, `attenuated`)."""

    path: Path
    audio_bitrate_kbps: int
    true_peak_before: float
    true_peak_after: float
    action: str
    attenuation_db: float = 0.0


async def enforce_true_peak(
    video: Path,
    mix: Path,
    out_dir: Path,
    *,
    ceiling_dbtp: float,
    bitrate_kbps: int,
    retry_bitrates_kbps: Sequence[int] = (),
    tolerance_db: float = 0.05,
) -> TruePeakFix:
    """The delivered file must meet the true-peak target, not just the mix (§27): AAC encoding can
    overshoot a limited mix by several dB at some bitrates. Measures the encoded audio; when it is
    over `ceiling_dbtp`, re-encodes only the audio — from the mix, the video stream copied — at each
    of `retry_bitrates_kbps` in turn, and as a last resort attenuates by the remaining overshoot (the
    loudness then falls below target, and the result says so)."""
    before = (await measure_loudness(video)).true_peak_dbtp
    if before <= ceiling_dbtp + tolerance_db:
        return TruePeakFix(video, bitrate_kbps, before, before, "none")
    out_dir.mkdir(parents=True, exist_ok=True)

    async def remux(kbps: int, gain_db: float, name: str) -> tuple[Path, float]:
        target = out_dir / name
        audio = ["-af", f"volume={gain_db:.2f}dB"] if gain_db else []
        await run_ffmpeg(
            ["-i", str(video), "-i", str(mix), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             *audio, "-c:a", "aac", "-b:a", f"{kbps}k", "-ar", "48000", "-ac", "2",
             "-movflags", "+faststart", "-shortest", str(target)]
        )  # fmt: skip
        return target, (await measure_loudness(target)).true_peak_dbtp

    best: tuple[Path, float, int] | None = None
    for kbps in retry_bitrates_kbps:
        path, peak = await remux(kbps, 0.0, f"tp_{kbps}k.mp4")
        if peak <= ceiling_dbtp + tolerance_db:
            return TruePeakFix(path, kbps, before, peak, "bitrate")
        if best is None or peak < best[1]:
            best = (path, peak, kbps)
    kbps = best[2] if best else bitrate_kbps
    overshoot = (best[1] if best else before) - ceiling_dbtp
    gain = -round(overshoot + 0.2, 2)
    path, peak = await remux(kbps, gain, f"tp_{kbps}k_attenuated.mp4")
    return TruePeakFix(path, kbps, before, peak, "attenuated", attenuation_db=-gain)
