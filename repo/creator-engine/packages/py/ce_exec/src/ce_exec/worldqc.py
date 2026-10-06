"""World continuity QC, CPU part (§19.6, Phase 7): `world_identity` and `world_lighting` per shot.

- `world_identity`: `image.embed` cosine similarity between the shot's background (the person
  region masked using `face.detect`, expanded to the body) and the canonical plate of the bound
  camera position, against the world kind's `min_environment_identity`.
- `world_lighting`: correlated colour temperature (McCamy's approximation from the mean linear sRGB)
  and luminance of the masked shot frame vs the plate, against the World DNA tolerance; the key-light
  side estimated from face shading (left/right luminance of the face box) vs the DNA key azimuth.

Phase 11 adds `world_elements` (a structured VLM question per shot: the camera position's visible
and must-show elements, their states and left/right relations; `ce_world.continuity`) when a VLM
passes the hard filters, and `qc.continuity` (video level): adjacent shots of the same binding
compared by background embedding and colour statistics. Cross-video world scores are part of the
consistency report (§20).

Every check is a measured score with confidence. `config/qc/world.yaml` marks them `gate: false`
until calibrated against human ratings, so they only warn.
"""

from __future__ import annotations

import statistics
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_render.ffmpeg import probe, run_ffmpeg
from ce_world.continuity import background_continuity, element_expectations, element_question, score_elements
from PIL import Image, ImageDraw

from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput

__all__ = ["cct_mccamy", "qc_continuity_node", "qc_world_node"]


def _linear(rgb: np.ndarray) -> np.ndarray:
    c = rgb / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def cct_mccamy(rgb_mean: np.ndarray) -> float:
    """Correlated colour temperature (K) of a mean sRGB colour (0–255)."""
    r, g, b = _linear(np.asarray(rgb_mean, dtype=float))
    x_ = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y_ = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z_ = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = x_ + y_ + z_
    if total <= 1e-9:
        return 0.0
    x, y = x_ / total, y_ / total
    n = (x - 0.3320) / (0.1858 - y) if abs(0.1858 - y) > 1e-9 else 0.0
    return float(449 * n**3 + 3525 * n**2 + 6823.3 * n + 5520.33)


def _luma(rgb: np.ndarray) -> float:
    return float(np.mean(0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]) / 255.0)


def _person_mask(size: tuple[int, int], boxes: list[list[float]]) -> Image.Image:
    """White = excluded: each face box widened to the shoulders and extended to the bottom edge."""
    w, h = size
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    for x, y, bw, bh, *_ in boxes:
        x0 = max(0.0, x - bw * 0.9) * w
        x1 = min(1.0, x + bw * 1.9) * w
        y0 = max(0.0, y - bh * 0.35) * h
        draw.rectangle([x0, y0, x1, h], fill=255)
    return mask


async def qc_world_node(run: NodeRun) -> NodeOutput:
    cfg = run.svc.bundle.qc_world
    routes = dict(run.node.params.get("metrics", {}))
    shot = run.dep("post.realism:")
    plate_out = run.maybe("world.plate:")
    checks: list[dict[str, Any]] = []
    video = await run.fetch(shot.ref("video"))
    info = await probe(video)
    frame_png = run.scratch / "frame.png"
    await run_ffmpeg(
        ["-ss", f"{max(0.0, (info.duration_s or 0.0) / 2):.3f}", "-i", str(video), "-frames:v", "1", str(frame_png)]
    )
    frame_ref = await run.write(frame_png, "image", role="qc_frame", mime="image/png")
    frame = np.asarray(Image.open(frame_png).convert("RGB"), dtype=np.float64)
    boxes: list[list[float]] = []
    if routes.get("face.detect"):
        detector = await run.svc.adapter(str(routes["face.detect"]["adapter_id"]))
        found = await detector.run("face.detect", m.MediaAnalysisRequest(media=frame_ref, sample_hz=1.0), run.ctx)
        boxes = [b for f in found.frames for b in f.get("boxes", [])][:3]  # type: ignore[attr-defined]
    mask = _person_mask((frame.shape[1], frame.shape[0]), boxes)
    keep = np.asarray(mask) < 128
    background = frame[keep] if keep.any() else frame.reshape(-1, 3)
    binding = run.scene.world
    world = run.data.refs.worlds.get(binding.world_version_id) if binding is not None else None
    dna: dict[str, Any] = dict(world.dna) if world is not None else {}
    kind = str(dna.get("kind", "default"))
    thresholds = (
        (cfg.thresholds_by_world_kind.get(kind) or cfg.thresholds_by_world_kind.get("default") or {}) if cfg else {}
    )
    min_identity = float(thresholds.get("min_environment_identity", 0.75))
    conf = {name: c for name, c in (cfg.checks.items() if cfg else [])}
    identity: float | None = None
    background_vector: list[float] = []
    if plate_out is not None and routes.get("image.embed"):
        mask_path = run.scratch / "mask.png"
        mask.save(mask_path)
        mask_ref = await run.write(mask_path, "image", role="qc_mask", mime="image/png")
        embedder = await run.svc.adapter(str(routes["image.embed"]["adapter_id"]))
        result = await embedder.run(
            "image.embed", m.ImageEmbedRequest(images=[frame_ref, plate_out.ref("image")], masks=[mask_ref]), run.ctx
        )
        vectors = [np.asarray(v, dtype=float) for v in result.vectors]  # type: ignore[attr-defined]
        if vectors:
            background_vector = [round(float(x), 6) for x in vectors[0]]
        if len(vectors) == 2 and np.linalg.norm(vectors[0]) and np.linalg.norm(vectors[1]):
            identity = float(vectors[0] @ vectors[1] / (np.linalg.norm(vectors[0]) * np.linalg.norm(vectors[1])))
        checks.append(
            {
                "check": "world_identity",
                "score": None if identity is None else round(identity, 4),
                "threshold": min_identity,
                "passed": None if identity is None else identity >= min_identity,
                "method": f"image.embed:{routes['image.embed']['adapter_id']}",
                "reliability": str(getattr(conf.get("world_identity"), "reliability", "medium")),
                "gate": bool(getattr(conf.get("world_identity"), "gate", False)),
                "masked_faces": len(boxes),
            }
        )
    if routes.get("vision.image") and binding is not None and dna:
        expectations = element_expectations(dna, binding.camera_position_key)
        if expectations["elements"]:
            question, schema = element_question(expectations)
            vlm = await run.svc.adapter(str(routes["vision.image"]["adapter_id"]))
            answer = await vlm.run(
                "vision.image",
                m.VisionRequest(media=frame_ref, question=question, json_schema=schema, labels={"node": run.node.key}),
                run.ctx,
            )
            judged = score_elements(dict(answer.answer), expectations)  # type: ignore[attr-defined]
            checks.append(
                {
                    "check": "world_elements",
                    "score": judged["score"],
                    "passed": None if judged["score"] is None else not judged["missing"],
                    **{k: v for k, v in judged.items() if k != "score"},
                    "method": f"vlm_structured:{routes['vision.image']['adapter_id']}",
                    "confidence": float(getattr(answer, "confidence", 0.0)),
                    "mock": bool(dict(answer.answer).get("mock")),  # type: ignore[attr-defined]
                    "reliability": str(getattr(conf.get("world_elements"), "reliability", "low")),
                    "gate": bool(getattr(conf.get("world_elements"), "gate", False)),
                }
            )
    lighting = dict(dna.get("lighting", {}))
    tolerance = dict(lighting.get("tolerance", {}))
    shot_cct, shot_luma = cct_mccamy(background.mean(axis=0)), _luma(background)
    lighting_check: dict[str, Any] = {
        "check": "world_lighting",
        "shot_cct_k": round(shot_cct),
        "shot_luminance": round(shot_luma, 4),
        "method": "mccamy_cct_and_luma",
        "reliability": str(getattr(conf.get("world_lighting"), "reliability", "low")),
        "gate": bool(getattr(conf.get("world_lighting"), "gate", False)),
    }
    if plate_out is not None:
        plate = np.asarray(Image.open(await run.fetch(plate_out.ref("image"))).convert("RGB"), dtype=np.float64)
        plate_cct, plate_luma = cct_mccamy(plate.reshape(-1, 3).mean(axis=0)), _luma(plate)
        cct_delta, luma_delta = shot_cct - plate_cct, shot_luma - plate_luma
        lighting_check.update(
            {
                "plate_cct_k": round(plate_cct),
                "plate_luminance": round(plate_luma, 4),
                "cct_delta_k": round(cct_delta),
                "luminance_delta": round(luma_delta, 4),
                "passed": abs(cct_delta) <= float(tolerance.get("color_temp_k", 400))
                and abs(luma_delta) <= float(tolerance.get("luminance", 0.12)),
            }
        )
    key = dict(lighting.get("key", {}))
    if boxes and key:
        x, y, bw, bh, *_ = boxes[0]
        h, w = frame.shape[:2]
        x0, x1 = int(max(0, x * w)), int(min(w, (x + bw) * w))
        y0, y1 = int(max(0, y * h)), int(min(h, (y + bh) * h))
        face = frame[y0:y1, x0:x1]
        if face.size and face.shape[1] >= 4:
            left, right = _luma(face[:, : face.shape[1] // 2]), _luma(face[:, face.shape[1] // 2 :])
            ratio = (left + 1e-6) / (right + 1e-6)
            measured = "left" if ratio > 1.15 else ("right" if ratio < 1 / 1.15 else "front")
            azimuth = float(key.get("azimuth_deg", 0.0))
            expected = "left" if azimuth < -15 else ("right" if azimuth > 15 else "front")
            lighting_check.update(
                {"key_side": measured, "expected_key_side": expected, "face_lr_ratio": round(ratio, 3)}
            )
    checks.append(lighting_check)
    weights = dict(cfg.weights) if cfg else {}
    scored = [(weights.get(c["check"], 0.0), c["score"]) for c in checks if c.get("score") is not None]
    environment = (
        round(sum(w * s for w, s in scored) / sum(w for w, _ in scored), 4)
        if scored and sum(w for w, _ in scored)
        else None
    )
    warnings = [c["check"] for c in checks if c.get("passed") is False]
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "checks": checks,
            "environment_identity": environment,
            "min_environment_identity": min_identity,
            "warnings": warnings,
            "gate": "warn (checks warn until calibrated against human ratings)",
            "binding": (
                f"{binding.world_version_id}:{binding.camera_position_key}:{binding.time_of_day}"
                if binding is not None
                else None
            ),
            "background": {
                "vector": background_vector,
                "rgb_mean": [round(float(c), 3) for c in background.mean(axis=0)],
            },
        },
        refs={"frame": frame_ref},
    )


async def qc_continuity_node(run: NodeRun) -> NodeOutput:
    """`qc.continuity` (§19.6 `background_continuity`, scope video): adjacent shots of the same
    binding, in timeline order."""
    final = run.dep("render.final:")
    order = sorted(dict(final.data.get("shots", {})).items(), key=lambda kv: (float(kv[1][0]), kv[0]))
    worlds = {k.split(":", 1)[1]: o for k, o in run.deps("qc.world:")}
    shots = [
        {
            "shot_key": key,
            "binding": worlds[key].data.get("binding"),
            "vector": dict(worlds[key].data.get("background") or {}).get("vector"),
            "rgb_mean": dict(worlds[key].data.get("background") or {}).get("rgb_mean"),
        }
        for key, _ in order
        if key in worlds
    ]
    result = background_continuity(shots)
    cfg = run.svc.bundle.qc_world
    check = cfg.checks.get("background_continuity") if cfg else None
    identities = [float(o.data["environment_identity"]) for o in worlds.values() if o.data.get("environment_identity")]
    weights = dict(cfg.weights) if cfg else {}
    parts = [(weights.get("background_continuity", 0.0), result["score"])] if result["score"] is not None else []
    if identities:
        parts.append((sum(w for k, w in weights.items() if k != "background_continuity"), statistics.fmean(identities)))
    total = sum(w for w, _ in parts)
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            **result,
            "reliability": str(getattr(check, "reliability", "medium")),
            "gate": bool(getattr(check, "gate", False)),
            # the video's environment identity: per-shot identity and continuity, weighted (§19.6)
            "environment_identity": round(sum(w * v for w, v in parts) / total, 4) if total else None,
        },
    )
