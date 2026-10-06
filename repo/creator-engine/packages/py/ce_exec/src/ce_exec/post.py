"""Shot and audio post executors (§19.7, §22, §27; Phase 7): camera post with procedural motion and
subject-aware reframing, realism post, per-scene world acoustics, and the reframing helpers the
final render uses for aspect variants.

In-process analyzers (face detection for reframing) are routes stamped on the node at graph time
(`params.metrics`, like QC metrics), so a different detector means a different cache key.
"""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from typing import Any

import numpy as np
from ce_camera.motion import exposure_expression, focus_hunts, motion_for
from ce_camera.reframe import SubjectTrack, crop_loss, plan_crop, track_from_detections
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_core.spec.anchors import WordRef
from ce_realism.audio import ambient_bed, process_dialogue
from ce_realism.video import realism_plan
from ce_render.audio import SAMPLE_RATE, decode, encode_wav
from ce_render.ffmpeg import probe
from ce_render.video import camera_post, realism_post

from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput

__all__ = [
    "audio_room_node",
    "placement_framing",
    "post_camera_node",
    "post_realism_node",
    "subject_track",
]

HANDHELD_DRIFT_FRACTION = 0.006  # `handheld_drift` amplitude per unit of `scale`, as a share of the height
CADENCE_S = {"fast": 2.5, "medium": 5.0}  # `scenes[].pacing.cut_cadence`: editorial reframes (no re-render)
CADENCE_SCALE = 1.08
REFRAME_HZ = 2.0


def _seed(run: NodeRun) -> int:
    if run.node.seed_base is not None:
        return int(run.ctx.seed or run.node.seed_base)
    return int(hashlib.sha256(run.node.key.encode()).hexdigest()[:8], 16)


def _main_input(run: NodeRun) -> tuple[ArtifactRef, dict[str, Any]] | None:
    for prefix in ("video.interpolate:", "video.upscale:", "post.expression:", "screen.prepare:"):
        out = run.maybe(prefix)
        if out is not None:
            return out.ref("video"), dict(out.data)
    return None


def _cadence_cuts(run: NodeRun, words: list[Any], explicit: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Editorial pacing (§28 `set_pacing`): alternating punch-in/out cuts at the scene's cut cadence
    over the shot, away from explicit punch-ins."""
    scene = run.scene
    cadence = CADENCE_S.get(str(scene.pacing.cut_cadence)) if scene.pacing and scene.pacing.cut_cadence else None
    if cadence is None or not words:
        return []
    start, end = float(words[0][0]), float(words[-1][1])
    out: list[tuple[float, float]] = []
    tight = True
    t = start + cadence
    while t < end - 0.5:
        if all(abs(t - e) > 0.75 for e, _ in explicit):
            out.append((round(t, 3), CADENCE_SCALE if tight else 1.0))
            tight = not tight
        t += cadence
    return out


async def subject_track(run: NodeRun, video: ArtifactRef, *, hz: float = REFRAME_HZ) -> tuple[SubjectTrack, str | None]:
    """Face-detector track of `video` through the node's `face.detect` route (empty without one)."""
    identity = dict(run.node.params.get("metrics", {})).get("face.detect")
    if not identity:
        return SubjectTrack(), None
    adapter = await run.svc.adapter(str(identity["adapter_id"]))
    result = await adapter.run("face.detect", m.MediaAnalysisRequest(media=video, sample_hz=hz), run.ctx)
    return track_from_detections(result.frames), str(identity["adapter_id"])  # type: ignore[attr-defined]


def _subject_summary(
    track: SubjectTrack, path: tuple[tuple[float, float, float], ...], width: float, height: float
) -> dict[str, float] | None:
    """The subject's mean box in the output frame (normalized), for later aspect variants."""
    if not track.found:
        return None
    cx = cy = w = h = 0.0
    for t, sx, sy, sw, sh in track.samples:
        x0, y0 = next(((px, py) for pt, px, py in reversed(path) if pt <= t), (path[0][1], path[0][2]))
        cx += (sx - x0) / width
        cy += (sy - y0) / height
        w += sw / width
        h += sh / height
    n = len(track.samples)
    return {"cx": round(cx / n, 4), "cy": round(cy / n, 4), "w": round(w / n, 4), "h": round(h / n, 4)}


async def post_camera_node(run: NodeRun) -> NodeOutput:
    from ce_exec.requests import select_take, take_video

    found = _main_input(run)
    if found is None:  # B-roll without upscale: choose the take here
        take, scores = select_take(run)
        video = await take_video(run, take)
        broll = [o for k, o in run.deps("video.broll:") if k.endswith(f":t{take}")]
        data = {**(broll[0].data if broll else {}), "selected_take": take, "scores": scores}
    else:
        video, data = found
    shot = run.shot
    words = data.get("shot_words") or []
    flat = run.shot_word_list()
    punch_ins: list[tuple[float, float]] = []
    for move in shot.camera.moves:
        if str(move.type) not in ("punch_in", "punch_out") or not isinstance(move.at, WordRef):
            continue
        at = (move.at.segment_key, move.at.word)
        if at in flat and words:
            t = float(words[flat.index(at)][0])
            scale = float(move.scale or (1.12 if str(move.type) == "punch_in" else 1.0))
            punch_ins.append((t, scale if str(move.type) == "punch_in" else 1.0))
    params = run.node.params
    width, height, fps = int(params["width"]), int(params["height"]), float(params["fps"])
    punch_ins += _cadence_cuts(run, words, punch_ins)
    drift = 0.0
    for move in shot.camera.moves:
        if str(move.type) == "handheld_drift":
            drift = max(drift, HANDHELD_DRIFT_FRACTION * height * float(move.scale or 1.0))
    profile = run.svc.bundle.camera_profiles.get(shot.camera.profile_id)
    seed = _seed(run)
    source = await run.fetch(video)
    info = await probe(source)
    duration = float(info.duration_s or 1.0)
    motion = motion_for(profile, seed)
    reframe = run.spec.render.reframe
    track, detector = (
        await subject_track(run, video) if str(reframe.strategy) == "subject_aware" else (SubjectTrack(), None)
    )
    eye_line = float(profile.framing.eye_line) if profile is not None else 0.38
    src_aspect = (info.width or width) / max(1, info.height or height)
    crop = plan_crop(
        track,
        src_aspect=src_aspect,
        dst_aspect=width / height,
        eye_line=eye_line,
        threshold=float(reframe.regenerate_if_crop_loss_above),
        strategy=str(reframe.strategy),
    )
    hunts = focus_hunts(float(profile.focus.autofocus_hunts_per_min), duration, seed) if profile is not None else []
    exposure = exposure_expression(float(profile.exposure.auto_exposure_drift), seed) if profile is not None else None
    speed = motion.mean_speed_px_per_frame(height, fps, duration)
    blur = 0 if speed < 1.5 else (2 if speed < 4.0 else 3)  # motion blur by speed band
    out = await camera_post(
        source,
        run.scratch / "mezzanine.mp4",
        width=width,
        height=height,
        fps=fps,
        punch_ins=punch_ins,
        drift_px=drift,
        motion=motion,
        crop=crop,
        focus=hunts,
        exposure=exposure,
        blur_frames=blur,
        duration_s=duration,
    )
    result = await probe(out)
    ref = await run.write(out, "video", role="mezzanine", mime="video/mp4")
    keys = ("span_offset_s", "shot_words", "clip_duration_s", "selected_take", "scores", "screen")
    keep: dict[str, Any] = {k: data[k] for k in keys if k in data}
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            **keep,
            "duration_s": result.duration_s,
            "frame": [width, height],
            "punch_ins": punch_ins,
            "motion": {
                "profile": profile.id if profile is not None else None,
                "type": str(profile.motion.type) if profile is not None else "static",
                "peak_px": round(motion.peak_px(height, duration), 2),
                "blur_frames": blur,
                "focus_hunts": len(hunts),
                "exposure_drift": exposure is not None,
            },
            "reframe": {
                "strategy": str(reframe.strategy),
                "detector": detector,
                "subject_found": track.found,
                "samples": len(track.samples),
                "layout": crop.layout,
                "crop_loss": crop.loss,
                "regenerate_recommended": crop.loss > float(reframe.regenerate_if_crop_loss_above),
                "subject": _subject_summary(track, crop.path, crop.width, crop.height),
            },
        },
        refs={"video": ref},
    )


async def post_realism_node(run: NodeRun) -> NodeOutput:
    camera = run.dep("post.camera:")
    profile = run.svc.bundle.camera_profiles.get(run.shot.camera.profile_id)
    plan = realism_plan(profile, run.svc.bundle.root, _seed(run))
    out = await realism_post(await run.fetch(camera.ref("video")), run.scratch / "realism.mp4", plan)
    ref = await run.write(out, "video", role="mezzanine", mime="video/mp4")
    return NodeOutput(node_kind=run.node.kind, data={**camera.data, "realism": plan.summary}, refs={"video": ref})


def placement_framing(
    data: dict[str, Any], width: int, height: int, *, strategy: str, threshold: float
) -> tuple[tuple[float, float] | None, str, float]:
    """For an output frame of another aspect than the mezzanine: the crop focus (fractions of the
    free space), the layout (`crop` or `blurred_fill`) and the crop loss (§22 reframing)."""
    frame = data.get("frame") or [width, height]
    src_aspect, dst_aspect = float(frame[0]) / float(frame[1]), width / height
    if abs(src_aspect - dst_aspect) / dst_aspect < 0.02:
        return None, "crop", 0.0
    if dst_aspect <= src_aspect:
        win_w, win_h = dst_aspect / src_aspect, 1.0
    else:
        win_w, win_h = 1.0, src_aspect / dst_aspect
    subject = (data.get("reframe") or {}).get("subject") if strategy == "subject_aware" else None
    cx, cy, sw, sh = (subject["cx"], subject["cy"], subject["w"], subject["h"]) if subject else (0.5, 0.42, 0.3, 0.3)
    x0 = min(max(cx - win_w / 2, 0.0), 1.0 - win_w)
    y0 = min(max(cy - 0.15 * sh - 0.38 * win_h, 0.0), 1.0 - win_h)
    track = SubjectTrack(((0.0, cx, cy, sw, sh),))
    loss = crop_loss(track, win_w, win_h, [(0.0, x0, y0)])
    fx = x0 / (1.0 - win_w) if win_w < 1.0 else 0.5
    fy = y0 / (1.0 - win_h) if win_h < 1.0 else 0.5
    layout = "blurred_fill" if loss > threshold else "crop"
    return (round(fx, 4), round(fy, 4)), layout, round(loss, 4)


# ---------------------------------------------------------------------- acoustics (audio.room)


def _read_ir(path: Path) -> np.ndarray | None:
    if not path.is_file():
        return None
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
    ir = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if rate != SAMPLE_RATE:  # the committed IRs are 48 kHz; resample defensively
        ir = np.interp(np.linspace(0, len(ir) - 1, round(len(ir) * SAMPLE_RATE / rate)), np.arange(len(ir)), ir).astype(
            np.float32
        )
    return ir


def _acoustics(run: NodeRun) -> dict[str, Any]:
    """The scene's room, mic, ambient beds and noise floor (world DNA, scene overrides, spec)."""
    spec, scene, bundle = run.spec, run.scene, run.svc.bundle
    acoustics = spec.audio.acoustics
    room_id: str | None = None
    ambient: list[str] = []
    floor = -62.0
    if str(acoustics.source) == "override" and acoustics.room_profile:
        room_id = str(acoustics.room_profile)
    elif scene.world is not None:
        world = run.data.refs.worlds.get(scene.world.world_version_id)
        dna = dict(world.dna.get("acoustics", {})) if world is not None else {}
        room_id = dna.get("room_profile")
        ambient = [str(a) for a in dna.get("ambient", [])]
        floor = float(dna.get("noise_floor_db", floor))
        if scene.world.overrides.acoustics is not None:
            ambient += [str(a) for a in scene.world.overrides.acoustics.ambient_additions]
    room = bundle.rooms.get(room_id) if room_id else None
    if room is not None and not ambient:
        ambient = [room.default_ambient]
    mic_id = str(acoustics.mic_profile) if acoustics.mic_profile else None
    if mic_id is None:
        for shot in scene.shots:
            profile = bundle.camera_profiles.get(shot.camera.profile_id)
            if profile is not None:
                mic_id = profile.audio.mic_profile
                break
    return {
        "room": room,
        "mic": bundle.mic_profiles.get(mic_id) if mic_id else None,
        "ambient": ambient,
        "noise_floor_db": floor,
    }


async def audio_room_node(run: NodeRun) -> NodeOutput:
    """Per-scene world acoustics (§19.7, §27 mix step 1): each segment through the mic chain and the
    room, plus the scene's room-tone/ambient bed for the mix to place under the scene."""
    setup = _acoustics(run)
    room, mic = setup["room"], setup["mic"]
    ir = _read_ir(run.svc.bundle.root / "rooms" / room.impulse_response) if room is not None else None
    wet = float(room.wet_mix) if room is not None else 0.0
    refs: dict[str, ArtifactRef] = {}
    total = 0.0
    segments = run.deps("tts.segment:")
    for key, out in segments:
        segment = key.split(":", 1)[1]
        dry = (await decode(await run.fetch(out.ref("audio")), channels=1))[:, 0]
        processed = process_dialogue(dry, SAMPLE_RATE, mic=mic, ir=ir, wet_mix=wet)
        path = await encode_wav(processed, run.scratch / f"{segment}.wav")
        refs[segment] = await run.write(path, "audio", role="dialogue", mime="audio/wav")
        total += len(processed) / SAMPLE_RATE
    cfg = run.svc.bundle.app.render.timeline
    bed_s = total + cfg.segment_gap_s * max(0, len(segments) - 1) + cfg.lead_s + cfg.tail_s + 2.0
    bed = ambient_bed(setup["ambient"], bed_s, SAMPLE_RATE, noise_floor_db=setup["noise_floor_db"], seed=_seed(run))
    refs["bed"] = await run.write(
        await encode_wav(bed, run.scratch / "bed.wav"), "audio", role="room_tone", mime="audio/wav"
    )
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "room": room.id if room is not None else None,
            "rt60_s": float(room.rt60_s) if room is not None else None,
            "wet_mix": wet,
            "mic": mic.id if mic is not None else None,
            "ambient": setup["ambient"],
            "noise_floor_db": setup["noise_floor_db"],
            "bed_s": round(bed_s, 3),
            "segments": [k.split(":", 1)[1] for k, _ in segments],
        },
        refs=refs,
    )
