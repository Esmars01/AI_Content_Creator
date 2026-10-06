"""Behavior nodes (§15–§16) executed in process: `behavior.resolve`, `behavior.compile_voice`,
`behavior.compile_visual`, `behavior.keyframe_state`, `behavior.observe` (measurement),
`qc.shot` (per-take judgement and ranking) and `behavior.coverage` (viewer-level report).

The logic lives in `ce_behavior`; this module gathers the inputs from the version, the graph and
the upstream output documents, and shapes the output documents. Records derived from these
outputs (behavior observations, QC reports, coverage summary, measured profiles) are written by
the runtime's bookkeeping for executed and cached nodes alike (`ce_exec.behavior_records`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ce_behavior.calibration import calibrated_vocabulary
from ce_behavior.compiler import (
    CompileTarget,
    compile_behavior,
    coverage_downgrades,
    realized_methods,
    realized_targets,
)
from ce_behavior.coverage import coverage_report
from ce_behavior.directives import generation_digest, to_directives
from ce_behavior.inputs import cast_inputs as shared_cast_inputs
from ce_behavior.inputs import previous_characters
from ce_behavior.inputs import world_input as shared_world_input
from ce_behavior.judge import AudioEvidence, VisualEvidence, judge_audio, judge_visual
from ce_behavior.nodes import keyframe_state, keyframe_state_digest, visual_target, voice_target
from ce_behavior.observe import ChunkMeasurement, observed_behavior
from ce_behavior.qa import decide, take_behavior_score
from ce_behavior.resolve import CastInput, WorldInput, resolve
from ce_behavior.scene import SceneWords
from ce_contracts import models as m
from ce_core.behavior.cbs import CBSContent, RequestedControl
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.observed import AnalyzerRef, ItemObservation, ObservedBehavior
from ce_core.enums import ObservationVerdict
from ce_core.spec.videospec import Scene, Shot, VideoSpec

from ce_exec.context import VersionData
from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput

__all__ = [
    "cast_inputs",
    "compile_visual_node",
    "compile_voice_node",
    "coverage_node",
    "keyframe_state_node",
    "observe_node",
    "qc_behavior",
    "resolve_node",
    "world_input",
]

Word = tuple[str, int]
Key = tuple[str, str]


# ---------------------------------------------------------------------- inputs


def cast_inputs(data: VersionData, scene: Scene) -> dict[str, CastInput]:
    return shared_cast_inputs(data.spec, data.refs, scene)


def world_input(data: VersionData, scene: Scene) -> WorldInput | None:
    return shared_world_input(data.refs, scene)


def _cbs(output: NodeOutput) -> CBSContent:
    return CBSContent.model_validate(output.data["content"])


def _compiled(output: NodeOutput) -> CompiledBehavior:
    return CompiledBehavior.model_validate(output.data["compiled"])


def _mode_methods(run: NodeRun) -> frozenset[str]:
    mode = run.svc.bundle.modes.get(str(run.spec.meta.mode))
    return frozenset(mode.editorial_methods) if mode else frozenset()


def _compile_with_downgrades(
    run: NodeRun, cbs: CBSContent, target: CompileTarget, words: SceneWords
) -> tuple[CompiledBehavior, list[dict[str, str]]]:
    compiled = compile_behavior(cbs, target, vocab=run.svc.bundle.vocab, words=words)
    planned = run.node.params.get("planned_adapter")
    downgrades: list[dict[str, str]] = []
    if planned and planned in run.catalog.manifests:
        manifest = run.catalog.manifests[planned]
        planned_target = replace(
            target,
            route_digest=None,
            matrix=manifest.behavior_matrix,
            knobs=dict(manifest.knobs),
            validation=str(manifest.validation),
            unreliable=frozenset(),
        )
        downgrades = coverage_downgrades(
            compile_behavior(cbs, planned_target, vocab=run.svc.bundle.vocab, words=words), compiled
        )
    return compiled, downgrades


# ---------------------------------------------------------------------- resolve, compile, keyframe


async def resolve_node(run: NodeRun) -> NodeOutput:
    scene = run.scene
    content = resolve(
        run.spec,
        scene,
        cast=cast_inputs(run.data, scene),
        world=world_input(run.data, scene),
        vocab=run.svc.bundle.vocab,
        config=run.svc.bundle.app.behavior,
        previous_scene_characters=previous_characters(run.spec, scene),
    )
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "scene_key": scene.key,
            "content": content.model_dump(mode="json"),
            "cbs_content_digest": content.digest(),
        },
    )


def _node_target_fields(run: NodeRun) -> dict[str, Any]:
    return {
        "unreliable": frozenset(run.node.params.get("unreliable", [])),
        "editorial_methods": _mode_methods(run),
    }


async def compile_voice_node(run: NodeRun) -> NodeOutput:
    cbs = _cbs(run.dep("behavior.resolve:"))
    scene = run.scene
    segment_key = run.node.segment_key or ""
    route = run.node.route
    assert route is not None
    target, words = voice_target(
        run.spec,
        scene,
        segment_key,
        manifest=run.catalog.manifests[route.adapter_id],
        route=route,
        character_key=run.node.character_key,
        **_node_target_fields(run),
    )
    compiled, downgrades = _compile_with_downgrades(run, cbs, target, words)
    directives = to_directives(compiled, cbs, words=words, word_times={}, vocab=run.svc.bundle.vocab)
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "compiled": compiled.model_dump(mode="json", by_alias=True),
            "directives": directives.model_dump(mode="json"),
            "cbs_content_digest": cbs.digest(),
            "generation_digest": generation_digest(compiled, cbs),
            "downgrades": downgrades,
        },
    )


async def compile_visual_node(run: NodeRun) -> NodeOutput:
    from ce_core.spec.anchors import WordRef
    from ce_render.timeline import chunk_windows

    from ce_exec.requests import _chunk_counts

    cbs = _cbs(run.dep("behavior.resolve:"))
    scene, shot = run.scene, run.shot
    shot_words = run.shot_word_list()
    audio = run.shot_audio()
    counts = _chunk_counts(run, shot_words)
    chunk = int(run.node.params["chunk"])
    windows = chunk_windows(audio, counts)
    times = {w: (audio.words[i][0], audio.words[i][1]) for i, w in enumerate(shot_words)}
    route = run.node.route
    assert route is not None
    tts = run.graph.by_key().get(f"tts.segment:{shot_words[0][0]}") if shot_words else None
    tts_matrix = run.catalog.manifests[tts.route.adapter_id].behavior_matrix if tts is not None and tts.route else None
    first, last = run.node.params["range"]
    target, words = visual_target(
        run.spec,
        scene,
        shot,
        chunk,
        (WordRef(segment_key=first[0], word=int(first[1])), WordRef(segment_key=last[0], word=int(last[1]))),
        manifest=run.catalog.manifests[route.adapter_id],
        route=route,
        tts_matrix=tts_matrix,
        **_node_target_fields(run),
    )
    compiled, downgrades = _compile_with_downgrades(run, cbs, target, words)
    directives = to_directives(
        compiled, cbs, words=words, word_times=times, window=windows[chunk - 1], vocab=run.svc.bundle.vocab
    )
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "compiled": compiled.model_dump(mode="json", by_alias=True),
            "directives": directives.model_dump(mode="json"),
            "cbs_content_digest": cbs.digest(),
            "generation_digest": generation_digest(compiled, cbs),
            "downgrades": downgrades,
        },
    )


async def keyframe_state_node(run: NodeRun) -> NodeOutput:
    """The resolved behavior at the shot's first word (§15.7 `keyframe_conditioning`)."""
    cbs = _cbs(run.dep("behavior.resolve:"))
    data = keyframe_state(run.spec, run.scene, run.shot, cbs)
    return NodeOutput(
        node_kind=run.node.kind,
        data={**data, "cbs_content_digest": cbs.digest(), "generation_digest": keyframe_state_digest(data)},
    )


# ---------------------------------------------------------------------- measurement


async def observe_node(run: NodeRun) -> NodeOutput:
    """Measurement only (§16.3): face and body tracks of one take, per chunk, stitched."""
    from ce_exec.requests import take_video

    take = run.node.take or 1
    chunks = sorted(
        ((int(k.split(":c")[1].split(":")[0]), o) for k, o in run.deps("avatar.render:")), key=lambda c: c[0]
    )
    video = await take_video(run, take) if len(chunks) > 1 else chunks[0][1].ref("video")
    route = run.node.route
    assert route is not None
    body_route = dict(run.node.params.get("metrics", {})).get("body.landmarks")
    face_adapter = await run.svc.adapter(route.adapter_id)
    body_adapter = await run.svc.adapter(str(body_route["adapter_id"])) if body_route else None
    hz = float(run.node.params.get("sample_hz", 5.0))
    measurements: list[ChunkMeasurement] = []
    for _, out in chunks:
        sidecars = [out.refs["behavior_track"]] if "behavior_track" in out.refs else []
        request = m.MediaAnalysisRequest(
            media=out.ref("video"), sample_hz=hz, sidecars=sidecars, character_key=run.node.character_key
        )
        face = await face_adapter.run("face.landmarks", request, run.ctx)
        body = await body_adapter.run("body.landmarks", request, run.ctx) if body_adapter is not None else None
        window = out.data.get("window") or [0.0, float(out.data.get("duration_s", 0.0))]
        measurements.append(
            ChunkMeasurement(
                offset_s=float(window[0]),
                duration_s=float(window[1]) - float(window[0]),
                face=face,  # type: ignore[arg-type]
                body=body,  # type: ignore[arg-type]
            )
        )
    analyzers = [AnalyzerRef(capability="face.landmarks", adapter_id=route.adapter_id, revision=route.revision)]
    if body_route:
        analyzers.append(
            AnalyzerRef(
                capability="body.landmarks",
                adapter_id=str(body_route["adapter_id"]),
                revision=str(body_route["revision"]),
            )
        )
    observed = observed_behavior(
        take_sha256="sha256:" + video.sha256,
        character_key=run.node.character_key or "",
        chunks=measurements,
        analyzers=analyzers,
    )
    tracks = observed.tracks[0] if observed.tracks else None
    summary = {
        "events": len(tracks.events) if tracks else 0,
        "face_detected_ratio": tracks.face_detected_ratio if tracks else 0.0,
    }
    return NodeOutput(
        node_kind=run.node.kind,
        data={"observed": observed.model_dump(mode="json"), "summary": summary, "take": take},
        refs={"video": video},
    )


# ---------------------------------------------------------------------- judgement


def _controls(cbs: CBSContent) -> dict[Key, RequestedControl]:
    return {(c.item_ref, c.dimension): c for c in cbs.requested_controls}


def _window_s(
    control: RequestedControl, words: SceneWords, times: Mapping[Word, tuple[float, float]]
) -> tuple[float, float] | None:
    item_words = [w for w in words.words(control.span) if w in times]
    if not item_words:
        return None
    start = times[item_words[0]][0]
    end = times[item_words[-1]][1]
    if control.duration_ms is not None and "/events[" in control.item_ref:
        end = start + control.duration_ms / 1000.0
    return start, end


_CALIBRATIONS: dict[str, list[dict[str, Any]]] = {}


async def calibration_rows(run: NodeRun) -> list[dict[str, Any]]:
    """The measured proxy calibrations the graph stamped on this node (`params.calibration`, §16.2)."""
    import json

    sha = run.node.params.get("calibration")
    if not sha:
        return []
    if sha not in _CALIBRATIONS:
        _CALIBRATIONS[str(sha)] = list(json.loads(await run.svc.docs.raw(str(sha))).get("proxy_calibrations", []))
    return _CALIBRATIONS[str(sha)]


def _analyzer_map(analyzers: Iterable[Any]) -> dict[str, tuple[str, str]]:
    return {str(a.capability): (str(a.adapter_id), str(a.revision)) for a in analyzers}


def qc_behavior(run: NodeRun, take: int, calibrations: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any] | None:
    """Per-take judgement of the shot's visual items (§16.3, §16.5): observations, the behavior
    score for take ranking, and the Performance QA decision (recorded; executed in Phase 11).
    Proxies use the measured calibration of the analyzer revision that produced the tracks."""
    observe = run.maybe("behavior.observe:")
    resolve_out = run.maybe("behavior.resolve:")
    if observe is None or resolve_out is None:
        return None
    judge_cfg = run.svc.bundle.app.behavior.judge
    cbs = _cbs(resolve_out)
    scene, shot = run.scene, run.shot
    words = SceneWords.of(run.spec, scene)
    observed = ObservedBehavior.model_validate(observe.data["observed"])
    vocab = calibrated_vocabulary(run.svc.bundle.vocab, calibrations, _analyzer_map(observed.analyzers))
    tracks = next((t for t in observed.tracks if t.character_key == shot.character_key), None)
    evidence = VisualEvidence(tracks=tracks, available=frozenset(a.capability for a in observed.analyzers))
    chunk_outs = [o for k, o in run.deps("avatar.render:") if k.endswith(f":t{take}")]
    shot_words = run.shot_word_list()
    raw = chunk_outs[0].data.get("shot_words", []) if chunk_outs else []
    times = {w: (float(t[0]), float(t[1])) for w, t in zip(shot_words, raw, strict=False)}
    realizations = realized_methods([_compiled(o) for _, o in run.deps("behavior.compile_visual:")])
    shot_range = words.range(shot.span)
    observations: list[ItemObservation] = []
    for control in cbs.requested_controls:
        definition = vocab.dimensions.get(control.dimension)
        if definition is None or definition.channel != "visual" or control.character_key != shot.character_key:
            continue
        rng = words.range(control.span)
        if rng is None or shot_range is None or rng[1] < shot_range[0] or rng[0] > shot_range[1]:
            continue
        window = _window_s(control, words, times)
        if window is None:
            continue
        observations.append(judge_visual(control, window, evidence, vocab, judge_cfg))
    controls = _controls(cbs)
    report = coverage_report(
        {
            scene.key: cbs.model_copy(
                update={"requested_controls": [controls[(o.item_ref, o.dimension)] for o in observations]}
            )
        },
        realizations,
        vocab,
        stage="observed",
        observations={(o.item_ref, o.dimension): o for o in observations},
    )
    policy = run.svc.bundle.qc_behavior
    voice_locked = "voice" in cbs.constraints.locks
    audio = frozenset(k for k, d in run.svc.bundle.vocab.dimensions.items() if d.channel == "audio")
    decision = (
        decide(report.entries, controls, policy, voice_locked=voice_locked, audio_dimensions=audio) if policy else None
    )
    return {
        "score": take_behavior_score(observations, controls),
        "item_observations": [o.model_dump(mode="json") for o in observations],
        "outcomes": {f"{e.item_ref}|{e.dimension}": str(e.outcome) for e in report.entries if e.outcome},
        "levels": {f"{e.item_ref}|{e.dimension}": f"{e.compiled.level}|{e.compiled.method}" for e in report.entries},
        "decision": decision.as_dict() if decision else None,
        "signature": observed.signature.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------- viewer level


async def coverage_node(run: NodeRun) -> NodeOutput:
    """`behavior.coverage` (§16.4): every CBS item judged on what the viewer sees and hears."""
    spec = run.spec
    calibrations = await calibration_rows(run)
    vocab = run.svc.bundle.vocab
    judge_cfg = run.svc.bundle.app.behavior.judge
    timeline = run.timeline()
    cbs_by_scene = {k.split(":", 1)[1]: _cbs(o) for k, o in run.deps("behavior.resolve:")}
    compiled_docs = [_compiled(o) for k, o in run.deps("behavior.compile_")]
    targets = realized_targets(compiled_docs)
    realizations = {k: r for k, (r, _) in targets.items()}
    observed = {k: ObservedBehavior.model_validate(o.data["observed"]) for k, o in run.deps("behavior.observe:")}
    cameras = {k.split(":", 1)[1]: o for k, o in run.deps("post.camera:")}

    # The audio the viewer hears: the final mix with word times on the timeline.
    mix = run.dep("mix.audio:")
    global_times: dict[Word, tuple[float, float]] = {
        (seg, i): (float(a), float(b)) for seg, pairs in timeline.word_times.items() for i, (a, b) in enumerate(pairs)
    }
    order = [
        (seg, i)
        for scene in sorted(spec.scenes, key=lambda s: s.order)
        for seg in scene.segment_keys
        for i in range(len(timeline.word_times.get(seg, [])))
    ]
    prosody_route = dict(run.node.params.get("metrics", {})).get("audio.prosody")
    energy: list[float] = []
    energy_hz = 10.0
    available: set[str] = {"asr.align"}
    if prosody_route:
        analyzer = await run.svc.adapter(str(prosody_route["adapter_id"]))
        request = m.AudioAnalysisRequest(
            audio=mix.ref("audio"),
            word_timings=[
                m.AlignedWord(index=i, word=str(i), start_s=a, end_s=b)
                for i, (a, b) in enumerate(global_times[w] for w in order)
            ],
        )
        prosody = await analyzer.run("audio.prosody", request, run.ctx)
        energy = list(getattr(prosody, "series", {}).get("energy", []))
        energy_hz = float(getattr(prosody, "sample_hz", 10.0))
        available.add("audio.prosody")
    audio_analyzers = (
        {"audio.prosody": (str(prosody_route["adapter_id"]), str(prosody_route["revision"]))} if prosody_route else {}
    )
    audio_vocab = calibrated_vocabulary(vocab, calibrations, audio_analyzers)
    wpm = {str(k): float(v) for k, v in dict(run.node.params.get("wpm", {})).items()}
    requested_energy: dict[tuple[str, Word], float] = {}
    for cbs in cbs_by_scene.values():
        for directive in cbs.prosody_directives:
            for i in range(len(timeline.word_times.get(directive.segment_key, []))):
                requested_energy[(directive.character_key, (directive.segment_key, i))] = float(directive.energy)

    observations: dict[Key, ItemObservation] = {}
    executed: dict[Key, bool | None] = {}
    overlay_slots = [(slot.start_s, slot.end_s, slot.shot_key) for slot in timeline.overlays]
    for scene_key, cbs in cbs_by_scene.items():
        scene = spec.scene(scene_key)
        words = SceneWords.of(spec, scene)
        for control in cbs.requested_controls:
            key = (control.item_ref, control.dimension)
            definition = vocab.dimensions.get(control.dimension)
            item_words = words.words(control.span)
            global_window = _window_s(control, words, global_times)
            if definition is not None and definition.channel == "audio":
                evidence = AudioEvidence(
                    word_times=global_times,
                    order=order,
                    energy=energy,
                    energy_hz=energy_hz,
                    baseline_wpm=wpm.get(control.character_key, 150.0),
                    available=frozenset(available),
                    requested_energy={w: e for (ch, w), e in requested_energy.items() if ch == control.character_key},
                )
                observations[key] = judge_audio(control, item_words, evidence, audio_vocab, judge_cfg)
            else:
                observations[key] = _viewer_visual(
                    control,
                    scene,
                    words,
                    observed,
                    cameras,
                    global_window,
                    overlay_slots,
                    vocab,
                    judge_cfg,
                    calibrations,
                )
            realization = realizations.get(key)
            if realization is not None and global_window is not None:
                executed[key] = _executed(
                    str(realization.method),
                    control,
                    global_window,
                    overlay_slots,
                    cameras,
                    timeline,
                    scene,
                    words,
                    judge_cfg.overlay_hidden_ratio,
                    spec,
                )
    report = coverage_report(
        cbs_by_scene,
        realizations,
        vocab,
        stage="observed",
        version_id=run.data.version_id,
        observations=observations,
        executed=executed,
    )
    controls = {k: c for cbs in cbs_by_scene.values() for k, c in _controls(cbs).items()}
    policy = run.svc.bundle.qc_behavior
    voice_locked = any("voice" in c.constraints.locks for c in cbs_by_scene.values())
    audio = frozenset(k for k, d in run.svc.bundle.vocab.dimensions.items() if d.channel == "audio")
    decision = (
        decide(report.entries, controls, policy, voice_locked=voice_locked, audio_dimensions=audio) if policy else None
    )
    downgrades = [d for k, o in run.deps("behavior.compile_") for d in o.data.get("downgrades", [])]
    return NodeOutput(
        node_kind=run.node.kind,
        data={
            "report": report.model_dump(mode="json"),
            "summary": report.summary_counts(),
            "decision": decision.as_dict() if decision else None,
            # unresolved must-item failures and render defects flag the version for review (§16.5)
            "passed": decision is None or decision.action in ("pass", "warn"),
            "downgrades": downgrades,
            "items": len(report.entries),
            # The target each entry's coverage is attributed to (its engine route, §16.6).
            "targets": {f"{ref}|{dim}": target for (ref, dim), (_, target) in sorted(targets.items())},
        },
    )


def _base_shot(scene: Scene, words: SceneWords, control: RequestedControl) -> Shot | None:
    rng = words.range(control.span)
    if rng is None:
        return None
    for shot in scene.shots:
        if str(shot.layer) != "base" or shot.character_key != control.character_key:
            continue
        srng = words.range(shot.span)
        if srng is not None and srng[0] <= rng[0] <= srng[1]:
            return shot
    return None


def _viewer_visual(
    control: RequestedControl,
    scene: Scene,
    words: SceneWords,
    observed: Mapping[str, ObservedBehavior],
    cameras: Mapping[str, NodeOutput],
    global_window: tuple[float, float] | None,
    overlay_slots: list[tuple[float, float, str]],
    vocab: Any,
    judge_cfg: Any,
    calibrations: Sequence[Mapping[str, Any]] = (),
) -> ItemObservation:
    shot = _base_shot(scene, words, control)
    if shot is None or shot.key not in cameras:
        return judge_visual(control, (0.0, 0.0), VisualEvidence(tracks=None), vocab, judge_cfg)
    camera = cameras[shot.key]
    take = int(camera.data.get("selected_take", 1))
    take_obs = observed.get(f"behavior.observe:{shot.key}:t{take}")
    tracks = None
    analyzers: frozenset[str] = frozenset()
    if take_obs is not None:
        tracks = next((t for t in take_obs.tracks if t.character_key == control.character_key), None)
        analyzers = frozenset(a.capability for a in take_obs.analyzers)
        vocab = calibrated_vocabulary(vocab, calibrations, _analyzer_map(take_obs.analyzers))
    shot_words = list(words.words(shot.span))
    raw = camera.data.get("shot_words", [])
    times = {w: (float(t[0]), float(t[1])) for w, t in zip(shot_words, raw, strict=False)}
    window = _window_s(control, words, times)
    if window is None:
        return judge_visual(control, (0.0, 0.0), VisualEvidence(tracks=None), vocab, judge_cfg)
    observation = judge_visual(control, window, VisualEvidence(tracks=tracks, available=analyzers), vocab, judge_cfg)
    if global_window is not None and observation.verdict in (ObservationVerdict.CONFIRMED, ObservationVerdict.PARTIAL):
        hidden = _covered(global_window, overlay_slots)
        if hidden >= judge_cfg.overlay_hidden_ratio:  # the viewer sees the overlay, not the face
            return observation.model_copy(
                update={
                    "verdict": ObservationVerdict.NOT_OBSERVED,
                    "measures": {**observation.measures, "hidden_by_overlay": round(hidden, 4)},
                }
            )
    return observation


def _covered(window: tuple[float, float], slots: list[tuple[float, float, str]]) -> float:
    a, b = window
    if b <= a:
        return 0.0
    covered = sum(max(0.0, min(b, e) - max(a, s)) for s, e, _ in slots)
    return min(1.0, covered / (b - a))


def _executed(
    method: str,
    control: RequestedControl,
    window: tuple[float, float],
    overlay_slots: list[tuple[float, float, str]],
    cameras: Mapping[str, NodeOutput],
    timeline: Any,
    scene: Scene,
    words: SceneWords,
    ratio: float,
    spec: VideoSpec,
) -> bool | None:
    """`approximation_executed` from the EDL (§16.4): the editorial action happened where planned."""
    if method == "editorial_cutaway":
        return _covered(window, overlay_slots) >= min(ratio, 0.5) or any(
            s <= window[0] + 0.05 and e >= window[0] for s, e, _ in overlay_slots
        )
    if method == "editorial_punch":
        for key, camera in cameras.items():
            slot = timeline.shots.get(key)
            if slot is None:
                continue
            offset = float(slot.span_start_s) - float(camera.data.get("span_offset_s", 0.0))
            for t, _scale in camera.data.get("punch_ins", []):
                if abs(offset + float(t) - window[0]) <= 0.35:
                    return True
        return False
    if method == "caption_emphasis":
        return bool(spec.captions.enabled and str(spec.captions.highlight) != "none")
    if method == "sfx_cue":
        return any(abs(float(t) - window[0]) <= 0.5 for t in timeline.sfx.values())
    if method == "music_cue":
        return any(abs(float(a) - window[0]) <= 0.5 for a, _ in timeline.music.values())
    return None
