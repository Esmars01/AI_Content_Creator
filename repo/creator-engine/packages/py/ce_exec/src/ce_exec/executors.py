"""In-process node executors: CPU nodes (orchestrator) and render nodes (render worker), plus model
nodes routed to `cpu_inproc` adapters (§9). Each returns the node's output document."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_policy import burned_labels, resolve_visible_label
from ce_qc.thresholds import MetricVerdict, judge_metric
from ce_qc.thresholds import metric_score as qc_metric_score
from ce_qc.vlm_judge import judge_question, judge_schema, parse_judgement
from ce_render.audio import MixInput, mix
from ce_render.ffmpeg import measure_loudness, probe, run_ffmpeg
from ce_render.fonts import missing_glyphs, pick_font, resolve_chain
from ce_render.video import ComposeJob, Encode, Logo, Placement, Title, compose, enforce_true_peak, make_proxy

from ce_exec.behavior import (
    calibration_rows,
    compile_visual_node,
    compile_voice_node,
    coverage_node,
    keyframe_state_node,
    observe_node,
    qc_behavior,
    resolve_node,
)
from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput
from ce_exec.post import audio_room_node, placement_framing, post_camera_node, post_realism_node
from ce_exec.requests import build_request, take_video
from ce_exec.results import output_from_result
from ce_exec.screen import screen_prepare_node
from ce_exec.worldqc import qc_continuity_node, qc_world_node

__all__ = ["EXECUTORS", "run_inproc_model", "run_local"]


def _out(run: NodeRun, data: dict[str, Any] | None = None, **refs: ArtifactRef) -> NodeOutput:
    return NodeOutput(node_kind=run.node.kind, data=data or {}, refs=refs)


async def _call(run: NodeRun, adapter_id: str, capability: str, request: Any) -> Any:
    adapter = await run.svc.adapter(adapter_id)
    return await adapter.run(capability, request, run.ctx)


# ---------------------------------------------------------------------- behavior (ce_behavior)
# resolve, compile, keyframe state, observation and coverage live in ce_exec.behavior.


# ---------------------------------------------------------------------- assets reused as-is


async def _reuse(run: NodeRun) -> NodeOutput:
    asset = run.asset("source")
    kind = {"world.plate": "image", "image.keyframe": "image", "video.broll": "video"}.get(run.node.kind, "audio")
    role = {"image": "image", "video": "video", "audio": "audio"}[kind]
    ref = await run.adopt(asset, kind=kind, role=role)
    data: dict[str, Any] = {"reused_asset": True}
    if run.node.kind == "video.broll":
        info = await probe(await run.fetch(ref))
        data.update({"span_offset_s": 0.0, "clip_duration_s": info.duration_s, "shot_words": []})
    return _out(run, data, **{role: ref})


# ---------------------------------------------------------------------- measurement and QC (CPU)


async def _qc_shot(run: NodeRun) -> NodeOutput:
    """Per-take checks (§26): metric thresholds keyed by the adapter that measured them, the VLM
    judge, and the Performance QA judgement (§16.5). `passed` is the gate's input; the take score
    ranks takes (D44)."""
    take = run.node.take or 1
    video = await take_video(run, take)
    tier = run.svc.bundle.qc_tiers.get(str(run.spec.meta.quality_tier))
    languages = run.svc.bundle.languages.languages if run.svc.bundle.languages else {}
    entry = languages.get(str(run.spec.meta.language).split("-")[0])
    beta = bool(entry and str(entry.support) == "beta")
    labels = {"node": run.node.key, "qc_attempt": str(run.qc_retry)}
    metrics: dict[str, Any] = {}
    verdicts: list[MetricVerdict] = []
    routes = dict(run.node.params.get("metrics", {}))
    for capability, identity in sorted(routes.items()):
        if capability == "vision.video":
            continue
        adapter_id = str(identity["adapter_id"])
        result = await _call(run, adapter_id, capability, m.QCMetricRequest(media=video, labels=labels))
        metrics[capability] = result.model_dump(mode="json")
        if tier is not None:
            verdicts.append(judge_metric(tier, capability, adapter_id, result, beta_language=beta))
        else:  # no tier configuration: the adapter's own verdict, never gating
            verdicts.append(
                MetricVerdict(
                    capability, adapter_id, result.metric, float(result.score), result.passed, True, "no_thresholds"
                )
            )
    judge: dict[str, Any] | None = None
    if "vision.video" in routes and tier is not None:
        cfg = tier.checks.vlm_judge
        answer = await _call(
            run,
            str(routes["vision.video"]["adapter_id"]),
            "vision.video",
            m.VisionRequest(
                media=video,
                question=judge_question(),
                json_schema=judge_schema(),
                sampling_fps=cfg.sampling_fps,
                labels=labels,
            ),
        )
        judgement = parse_judgement(answer.answer, float(answer.confidence), min_confidence=cfg.min_confidence)
        judge = {
            **judgement.as_dict(),
            "adapter_id": str(routes["vision.video"]["adapter_id"]),
            "gating": bool(cfg.critical_defects_fail),
        }
    elif "vision.video" not in routes:
        judge = {"skipped": "no VLM passes the hard filters here", "gating": False, "critical": False, "defects": []}
    metric_score = qc_metric_score(verdicts)
    if judge and judge.get("critical"):
        metric_score = round(metric_score * 0.5, 4)  # a visible defect ranks the take down even when it only warns
    passed = not any(v.gating_failure for v in verdicts) and not (
        judge and judge.get("gating") and judge.get("critical")
    )
    behavior = qc_behavior(run, take, await calibration_rows(run))
    score = metric_score
    policy = run.svc.bundle.qc_behavior
    if behavior is not None and behavior["score"] is not None and policy is not None:
        weight = float(policy.take_ranking_weight)  # observation ranks takes with the other QC metrics (§16.5)
        score = round((1.0 - weight) * metric_score + weight * float(behavior["score"]), 4)
    data = {
        "take": take,
        "metrics": metrics,
        "verdicts": {v.capability: v.as_dict() for v in verdicts},
        "vlm_judge": judge,
        "metric_score": metric_score,
        "score": score,
        "passed": passed,
        "behavior": behavior,
        "gate": "shot",
    }
    return _out(run, data)


# ---------------------------------------------------------------------- shot post (render)
# camera post, realism post and world acoustics live in ce_exec.post.


# ---------------------------------------------------------------------- video level (render)


async def _captions(run: NodeRun) -> NodeOutput:
    timeline = run.timeline()
    spec = run.spec
    language = spec.captions.language or spec.meta.language
    style = run.svc.bundle.caption_styles[spec.captions.style_id].model_dump(mode="json")
    primary = spec.render.outputs[0].preset_id if spec.render.outputs else None
    safe_zone: dict[str, float] = {}
    for platform in run.svc.bundle.platforms.values():
        if primary and any(p.id == primary for p in platform.render_presets):
            safe_zone = platform.rules.caption_safe_zone.model_dump()
    languages = run.svc.bundle.languages.languages if run.svc.bundle.languages else {}
    entry = languages.get(language.split("-")[0])
    # The bundled font chain (§27): the style's first family that covers the text, and caption QC
    # of glyph coverage (a missing glyph would render as a box).
    text = " ".join(t for t, *_ in timeline.caption_words)
    chain = [str(style.get("font_family", "Noto Sans")), *[str(f) for f in style.get("font_fallbacks", [])]]
    style = {**style, "font_family": pick_font(chain, text)}
    missing = missing_glyphs(text, chain)
    request = m.CaptionBuildRequest(
        words=[m.CaptionWord(text=t, start_s=a, end_s=b, emphasis=e) for t, a, b, e in timeline.caption_words],
        language=language,
        rtl=bool(entry and entry.rtl),
        style=style,
        safe_zone=safe_zone,
        width=int(run.node.params.get("width", 1080)),
        height=int(run.node.params.get("height", 1920)),
        max_words_per_line=spec.captions.max_words_per_line,
        highlight=spec.captions.highlight,
        placement=spec.captions.placement,
    )
    route = run.node.route
    assert route is not None
    result = await _call(run, route.adapter_id, "captions.build", request)
    return output_from_result(
        run,
        "captions.build",
        result,
        {
            "language": language,
            "words": len(timeline.caption_words),
            "font": style["font_family"],
            "font_chain": resolve_chain(chain),
            "missing_glyphs": missing,
            "glyph_coverage_ok": not missing,
            "rtl": bool(entry and entry.rtl),
        },
    )


async def _mix(run: NodeRun) -> NodeOutput:
    timeline = run.timeline()
    spec = run.spec
    cfg = run.svc.bundle.app.render.ducking
    dialogue = []
    beds = []
    for key, room in run.deps("audio.room:"):
        for segment, ref in room.refs.items():
            if segment == "bed":
                continue
            dialogue.append((await run.fetch(ref), timeline.segment_offsets[segment]))
        if "bed" in room.refs:  # the scene's room tone and ambience, under the whole scene (§27 step 3)
            start, end = timeline.scenes.get(key.split(":", 1)[1], (0.0, timeline.total_s))
            beds.append((await run.fetch(room.ref("bed")), max(0.0, start), end if end > start else timeline.total_s))
    music = []
    for cue in spec.audio.music.cues:
        out = run.upstream.get(f"audio.music:{cue.key}")
        if out is not None:
            start, end = timeline.music[cue.key]
            music.append((await run.fetch(out.ref("audio")), start, end, float(cue.duck_db)))
    sfx = []
    for event in spec.audio.sfx:
        out = run.upstream.get(f"audio.sfx:{event.key}")
        if out is not None:
            sfx.append((await run.fetch(out.ref("audio")), timeline.sfx[event.key], float(event.gain_db)))
    loudness = spec.audio.loudness
    result = await mix(
        MixInput(
            dialogue=dialogue,
            music=music,
            sfx=sfx,
            beds=beds,
            speech=timeline.speech_intervals(cfg.speech_pad_s),
            total_s=timeline.total_s,
            integrated_lufs=float(loudness.integrated_lufs),
            true_peak_dbtp=float(loudness.true_peak_dbtp)
            - float(run.svc.bundle.app.render.true_peak_codec_headroom_db),
        ),
        run.scratch / "mix",
    )
    ref = await run.write(result.path, "audio", role="mix", mime="audio/wav")
    data = {
        "total_s": timeline.total_s,
        "integrated_lufs": result.measured.integrated_lufs,
        "true_peak_dbtp": result.measured.true_peak_dbtp,
        "lra": result.measured.lra,
        "normalization": result.normalization,
        "premix_lufs": result.premix.integrated_lufs if result.premix else None,
        "room_tone_beds": len(beds),
    }
    return _out(run, data, audio=ref)


def _encode(run: NodeRun) -> Encode:
    preset_id = run.node.key.split(":", 1)[1]
    preset = run.svc.bundle.render_preset(preset_id)
    assert preset is not None
    return Encode(
        width=preset.width,
        height=preset.height,
        fps=float(preset.fps),
        crf=int(preset.crf),
        max_bitrate_kbps=preset.max_bitrate_kbps,
        audio_bitrate_kbps=preset.audio_bitrate_kbps,
        preset=run.svc.bundle.app.render.encode_preset,
    )


async def _render_final(run: NodeRun) -> NodeOutput:
    timeline = run.timeline()
    spec = run.spec

    encode = _encode(run)
    reframe = spec.render.reframe
    framing: dict[str, Any] = {}

    async def placement(slot: Any) -> Placement | None:
        out = run.upstream.get(f"post.realism:{slot.shot_key}")
        if out is None:
            return None
        offset = float(out.data.get("span_offset_s", 0.0))
        focus, layout, loss = placement_framing(
            dict(out.data),
            encode.width,
            encode.height,
            strategy=str(reframe.strategy),
            threshold=float(reframe.regenerate_if_crop_loss_above),
        )
        if focus is not None:
            framing[slot.shot_key] = {"focus": list(focus), "layout": layout, "crop_loss": loss}
        bubble = (out.data.get("screen") or {}).get("bubble")
        return Placement(
            await run.fetch(out.ref("video")),
            slot.span_start_s - offset,
            slot.start_s,
            slot.end_s,
            focus,
            layout,
            (str(bubble["corner"]), float(bubble["size"])) if bubble and slot.layer != "base" else None,
        )

    base = [p for p in [await placement(s) for s in timeline.base] if p is not None]
    overlays = [p for p in [await placement(s) for s in timeline.overlays] if p is not None]
    titles = [Title(text, slot.start_s, slot.end_s) for slot, text in timeline.titles]
    captions = run.maybe("captions.build:")
    regions = run.svc.catalog.operator.regions_served
    visible = resolve_visible_label(str(spec.provenance.visible_label), regions)
    mode = str(run.node.params.get("provenance_mode", run.svc.provenance_mode))
    labels = burned_labels(mode, visible)
    # On-screen disclosures (§32: `testimonial_dramatization` adds one) are burned with the labels.
    labels += [str(e.params["text"]) for e in spec.effects if e.type == "disclosure" and e.params.get("text")]
    logo = None
    if "logo" in run.node.asset_inputs:  # the brand kit's logo (spec.brand.logo_overlay, Phase 12)
        placement_cfg = run.svc.bundle.app.render.brand_logo
        logo = Logo(
            await run.fetch(await run.adopt(run.asset("logo"), kind="image", role="brand_logo")),
            corner=placement_cfg.corner,
            width_ratio=placement_cfg.width_ratio,
            margin_ratio=placement_cfg.margin_ratio,
            opacity=placement_cfg.opacity,
        )
    job = ComposeJob(
        base=base,
        overlays=overlays,
        titles=titles,
        audio=await run.fetch(run.dep("mix.audio:").ref("audio")),
        total_s=timeline.total_s,
        encode=encode,
        captions_ass=await run.fetch(captions.ref("ass")) if captions is not None and spec.captions.enabled else None,
        labels=labels,
        logo=logo,
    )
    out = await compose(job, run.scratch / "final.mp4")
    # the delivered file must meet the true peak, not only the mix: AAC can overshoot (§27)
    render_cfg = run.svc.bundle.app.render
    fix = await enforce_true_peak(
        out,
        job.audio,
        run.scratch / "true_peak",
        ceiling_dbtp=float(spec.audio.loudness.true_peak_dbtp),
        bitrate_kbps=encode.audio_bitrate_kbps,
        retry_bitrates_kbps=render_cfg.true_peak_retry_bitrates_kbps,
    )
    out = fix.path
    info = await probe(out)
    ref = await run.write(out, "video", role="render", mime="video/mp4")
    data = {
        "duration_s": info.duration_s,
        "width": info.width,
        "height": info.height,
        "fps": info.fps,
        "labels": labels,
        "logo": logo is not None,
        "provenance_mode": mode,
        "visible_label": visible,
        "reframing": framing,
        # the delivered audio: bitrate, true peak before and after the delivery check, what it did
        "audio": {
            "bitrate_kbps": fix.audio_bitrate_kbps,
            "true_peak_encoded_dbtp": round(fix.true_peak_before, 2),
            "true_peak_dbtp": round(fix.true_peak_after, 2),
            "true_peak_action": fix.action,
            "attenuation_db": fix.attenuation_db,
        },
        # where each shot plays on the final timeline (the critique and QC reports point at times)
        "shots": {
            slot.shot_key: [round(slot.start_s, 3), round(slot.end_s, 3)]
            for slot in [*timeline.base, *timeline.overlays]
        },
    }
    return _out(run, data, video=ref)


async def _watermark(run: NodeRun) -> NodeOutput:
    final = run.dep("render.final:")
    route = run.node.route
    assert route is not None
    payload_id = "ce1:" + final.ref("video").sha256[:24]
    request = m.WatermarkRequest(media=final.ref("video"), payload_id=payload_id)
    result = await _call(run, route.adapter_id, run.node.kind, request)
    return output_from_result(run, run.node.kind, result, {})


async def _sign(run: NodeRun) -> NodeOutput:
    """Chain the layers (§27): the watermarked frames and the watermarked mix are muxed into one
    file, then the C2PA manifest is signed over it. The render is `real` only when every layer is."""
    video = run.dep("provenance.watermark_video:")
    audio = run.maybe("provenance.watermark_audio:")
    route = run.node.route
    assert route is not None
    media = video.ref("media")
    if audio is not None and audio.ref("media").sha256 != media.sha256:
        muxed = run.scratch / "watermarked.mp4"
        await run_ffmpeg(
            ["-i", str(await run.fetch(media)), "-i", str(await run.fetch(audio.ref("media"))), "-map", "0:v:0",
             "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", str(muxed)]
        )  # fmt: skip
        media = await run.write(muxed, "video", role="watermarked", mime="video/mp4")
    routes = sorted(
        {
            f"{n.route.adapter_id}@{n.route.revision}"
            for n in run.graph.nodes
            if n.route is not None and n.executor == "model"
        }
    )
    layers = {
        "watermark_video": str(video.data.get("mode", "mock_dev")),
        "watermark_audio": str(audio.data.get("mode", "mock_dev")) if audio is not None else "absent",
    }
    footage = any(n.asset_inputs.get("source") and n.kind in ("video.broll", "screen.prepare") for n in run.graph.nodes)
    manifest = {
        "digital_source_type": "compositeWithTrainedAlgorithmicMedia" if footage else "trainedAlgorithmicMedia",
        "ingredients": routes,
        "consent_ids": [str(c) for c in run.spec.provenance.consent_ids],
        "watermark_payload_id": video.data.get("payload_id"),
        "watermarks": layers,
    }
    request = m.SignRequest(media=media.model_copy(update={"mime": "video/mp4"}), manifest=manifest)
    result = await _call(run, route.adapter_id, "provenance.sign", request)
    signer_mode = str(getattr(result, "mode", "mock_dev"))
    overall = "real" if signer_mode == "real" and all(v == "real" for v in layers.values()) else "mock_dev"
    return output_from_result(
        run,
        "provenance.sign",
        result,
        {"payload_id": video.data.get("payload_id"), "mode": overall, "layers": {**layers, "c2pa": signer_mode}},
    )


async def _qc_render(run: NodeRun) -> NodeOutput:
    signed = run.dep("provenance.sign:")
    path = await run.fetch(signed.ref("media"))
    info = await probe(path)
    loud = await measure_loudness(path)
    target = run.spec.audio.loudness
    params = run.node.params
    checks = {
        "resolution": info.width == params.get("width") and info.height == params.get("height"),
        "fps": abs((info.fps or 0) - float(params.get("fps", 0))) < 0.01,
        "loudness": abs(loud.integrated_lufs - float(target.integrated_lufs)) <= 1.0,
        "true_peak": loud.true_peak_dbtp <= float(target.true_peak_dbtp) + 0.05,  # measurement resolution
        "audio": info.has_audio,
    }
    data = {
        "media": info.as_dict(),
        "integrated_lufs": loud.integrated_lufs,
        "true_peak_dbtp": loud.true_peak_dbtp,
        "checks": checks,
        "passed": all(checks.values()),
        "gate": "render",  # a failing render check flags the version for review (§26)
    }
    return _out(run, data)


async def _proxy(run: NodeRun) -> NodeOutput:
    signed = run.dep("provenance.sign:")
    cfg = run.svc.bundle.app.render.proxy
    out = await make_proxy(await run.fetch(signed.ref("media")), run.scratch / "proxy.mp4", height=cfg.height)
    info = await probe(out)
    ref = await run.write(out, "video", role="proxy", mime="video/mp4")
    return _out(run, {"duration_s": info.duration_s, "width": info.width, "height": info.height}, video=ref)


async def run_inproc_model(run: NodeRun) -> NodeOutput:
    """A model node whose route is a `cpu_inproc` adapter (e.g. caption translation)."""
    prepared = await build_request(run)
    route = run.node.route
    assert route is not None and run.node.capability is not None
    result = await _call(run, route.adapter_id, run.node.capability, prepared.request)
    return output_from_result(run, run.node.capability, result, prepared.extra)


EXECUTORS: dict[str, Callable[[NodeRun], Awaitable[NodeOutput]]] = {
    "behavior.resolve": resolve_node,
    "behavior.compile_voice": compile_voice_node,
    "behavior.compile_visual": compile_visual_node,
    "behavior.keyframe_state": keyframe_state_node,
    "behavior.coverage": coverage_node,
    "behavior.observe": observe_node,
    "qc.shot": _qc_shot,
    "qc.world": qc_world_node,
    "screen.prepare": screen_prepare_node,
    "post.camera": post_camera_node,
    "post.realism": post_realism_node,
    "audio.room": audio_room_node,
    "captions.build": _captions,
    "mix.audio": _mix,
    "render.final": _render_final,
    "provenance.watermark_video": _watermark,
    "provenance.watermark_audio": _watermark,
    "provenance.sign": _sign,
    "qc.render": _qc_render,
    "qc.continuity": qc_continuity_node,
    "render.proxy": _proxy,
}


async def _reuse_output(run: NodeRun) -> NodeOutput:
    """A node that reuses an earlier output by content (`params.reuse`, the voice lock's pinned
    prosody, §12.7): the same document, so every consumer's cache key is unchanged."""
    previous = await run.svc.docs.output(str(run.node.params["reuse"]))
    if previous.node_kind != run.node.kind:
        raise ValueError(f"{run.node.key}: reused output is a {previous.node_kind} document")
    return previous


async def run_local(run: NodeRun) -> NodeOutput:
    node = run.node
    if node.params.get("reuse") is not None:
        return await _reuse_output(run)
    if (
        node.asset_inputs.get("source")
        and node.executor == "cpu"
        and node.kind
        in (
            "world.plate",
            "image.keyframe",
            "video.broll",
            "audio.music",
            "audio.sfx",
        )
    ):
        return await _reuse(run)
    if node.executor == "model":
        return await run_inproc_model(run)
    executor = EXECUTORS.get(node.kind)
    if executor is None:
        raise LookupError(f"no in-process executor for {node.kind}")
    return await executor(run)
