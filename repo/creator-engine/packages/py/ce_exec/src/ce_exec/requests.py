"""Request builders for model nodes (§12.1, §23): from a graph node and its upstream outputs to the
typed capability request a worker runs. Engine syntax never appears here: behavior directives stay
abstract and the plugin's translator turns them into engine parameters on the worker (I1).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ce_build.world import plate_view
from ce_contracts import models as m
from ce_contracts.behavior import BehaviorDirectives
from ce_contracts.common import ArtifactRef
from ce_core.text import tokenize
from ce_render.audio import assemble
from ce_render.timeline import chunk_windows
from ce_render.video import concat
from ce_voice.normalize import normalize
from pydantic import BaseModel

from ce_exec.noderun import NodeRun, artifact_refs_in

__all__ = ["Prepared", "build_request", "select_take", "take_video"]


@dataclass
class Prepared:
    request: BaseModel
    labels: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def inputs(self) -> list[ArtifactRef]:
        return artifact_refs_in(self.request)


def _language(run: NodeRun, segment_key: str) -> str:
    segment = run.spec.script.segment(segment_key)
    return segment.language or run.spec.meta.language


async def _voice_prepare(run: NodeRun) -> Prepared:
    character = run.node.character_key or ""
    member = next(c for c in run.spec.cast if c.key == character)
    creator = run.data.refs.creator(member.creator_version_id)
    voice_id = member.overrides.voice_version_id or creator.voice_version_id
    voice = run.data.refs.voices[voice_id]  # type: ignore[index]
    references = []
    for ref, asset in zip(voice.references, voice.reference_assets, strict=False):
        audio = await run.adopt(asset, kind="audio", role="voice_reference")
        references.append(
            m.VoiceReference(
                audio=audio,
                transcript=str(ref.get("transcript", "")),
                language=str(ref.get("language", run.spec.meta.language)),
            )
        )
    return Prepared(
        m.VoicePrepareRequest(references=references, description=voice.description, labels={"character": character})
    )


def _voice_of(run: NodeRun) -> Any:
    member = next(c for c in run.spec.cast if c.key == run.node.character_key)
    creator = run.data.refs.creator(member.creator_version_id)
    return run.data.refs.voices[member.overrides.voice_version_id or creator.voice_version_id]  # type: ignore[index]


async def _tts(run: NodeRun) -> Prepared:
    segment_key = run.node.segment_key or ""
    segment = run.spec.script.segment(segment_key)
    voice = _voice_of(run)
    compiled = run.dep("behavior.compile_voice:")
    prepare = run.maybe("voice.prepare:")
    directives = BehaviorDirectives.model_validate(compiled.data["directives"])
    lexicon = {str(e["term"]): str(e.get("respelling") or e["term"]) for e in voice.lexicon if e.get("term")}
    language = _language(run, segment_key)
    request = m.TTSRequest(
        text=segment.text,
        words=[t.text for t in tokenize(segment.text)],
        spoken=spoken_words(segment, language, lexicon),
        language=language,
        voice=m.VoiceConditioning(
            conditioning=prepare.ref("conditioning") if prepare is not None else None,
            description=voice.description,
            lexicon=lexicon,
        ),
        behavior=directives,
        sample_rate=int(run.node.params.get("sample_rate", 48_000)),
        wpm=float(run.node.params.get("wpm", 150.0)),
        labels={"segment": segment_key, "character": run.node.character_key or ""},
    )
    return Prepared(request)


def spoken_words(segment: Any, language: str, lexicon: dict[str, str]) -> list[str]:
    """Per canonical word, what the engine speaks (§21 normalization): numbers, dates and
    abbreviations spelled out, the voice lexicon applied, then the segment's pronunciation
    annotations (their respelling replaces the span; its other words are merged into it)."""
    pieces = list(normalize(segment.text, language, lexicon).pieces)
    for annotation in segment.annotations:
        if str(annotation.type) != "pronunciation" or not annotation.respelling:
            continue
        start, end = annotation.span.start, annotation.span.end
        if start.segment_key != segment.key:
            continue
        last = end.word if end.segment_key == segment.key else len(pieces) - 1
        if 0 <= start.word < len(pieces):
            pieces[start.word] = annotation.respelling
            for i in range(start.word + 1, min(last, len(pieces) - 1) + 1):
                pieces[i] = ""
    return pieces


async def _asr_verify(run: NodeRun) -> Prepared:
    segment_key = run.node.segment_key or ""
    audio = run.dep("tts.segment:").ref("audio")
    return Prepared(
        m.TranscribeRequest(audio=audio, language=_language(run, segment_key), labels={"segment": segment_key})
    )


async def _align(run: NodeRun) -> Prepared:
    segment_key = run.node.segment_key or ""
    segment = run.spec.script.segment(segment_key)
    audio = run.dep("tts.segment:").ref("audio")
    language = _language(run, segment_key)
    heard = normalize(segment.text, language)  # what an ASR hears: no lexicon respellings
    request = m.AlignRequest(
        audio=audio,
        text=segment.text,
        words=[t.text for t in tokenize(segment.text)],
        language=language,
        spoken_words=heard.words,
        spoken_sources=[t.source for t in heard.tokens],
        labels={"segment": segment_key},
    )
    return Prepared(request)


def _world_prompt(run: NodeRun) -> str:
    """From the plate view only (what the bound position shows, §19.5), so equal plate digests
    always mean equal requests."""
    binding = run.scene.world
    assert binding is not None
    world = run.data.refs.worlds[binding.world_version_id]
    dna = world.dna
    view = plate_view(binding, world)
    labels = {e.get("key"): e.get("label") or e.get("key") for e in dna.get("elements", [])}
    parts = [str(dna.get("name", "")), str(dna.get("kind", "")).replace("_", " ")]
    parts += [str(view["time_of_day"]).replace("_", " "), str(view["weather"])]
    parts += [f"{labels.get(k, k)} {state}" for k, state in view["element_states"].items()]
    parts += [f"without {labels.get(k, k)}" for k in view["hidden"]]
    parts += [f"with {a['label']}" for a in view["added"]]
    parts += [f"{labels.get(k, k)} moved" for k, _ in view["moved"]]
    lighting = view["lighting"]
    if lighting:
        parts.append(f"lighting {lighting['color_temp_k_delta']:+g}K {lighting['luminance_delta']:+g} luminance")
    return ", ".join(p for p in parts if p)


async def _world_plate(run: NodeRun) -> Prepared:
    width, height = int(run.node.params["width"]), int(run.node.params["height"])
    binding = run.scene.world
    assert binding is not None
    labels = {"kind": "plate", "camera": binding.camera_position_key, "time": str(binding.time_of_day)}
    prompt = _world_prompt(run)
    if run.node.capability == "image.generate":
        return Prepared(m.ImageGenerateRequest(prompt=prompt, width=width, height=height, labels=labels))
    base = await run.adopt(run.asset("base_plate"), kind="image", role="base_plate")
    return Prepared(m.ImageEditRequest(image=base, prompt=prompt, width=width, height=height, labels=labels))


async def _keyframe(run: NodeRun) -> Prepared:
    shot = run.shot
    character = shot.character_key or ""
    member = next(c for c in run.spec.cast if c.key == character)
    creator = run.data.refs.creator(member.creator_version_id)
    appearance_id = member.overrides.appearance_version_id or creator.appearance_version_id
    appearance = run.data.refs.appearances.get(appearance_id) if appearance_id else None
    state = run.dep("behavior.keyframe_state:").data
    plate = run.maybe("world.plate:")
    references: list[ArtifactRef] = []
    if appearance is not None and appearance.canonical_face is not None:
        references.append(await run.adopt(appearance.canonical_face, kind="image", role="canonical_face"))
    scene_cast = next((c for c in run.scene.cast if c.character_key == character), None)
    wardrobe = (
        run.data.refs.wardrobes.get(scene_cast.wardrobe_version_id)
        if scene_cast and scene_cast.wardrobe_version_id
        else None
    )
    if wardrobe is not None:
        for asset in wardrobe.reference_assets:
            references.append(await run.adopt(asset, kind="image", role="wardrobe_reference"))
    profile = run.svc.bundle.camera_profiles.get(shot.camera.profile_id)
    dna = dict(appearance.dna) if appearance is not None else {}
    prompt_parts = [
        creator.display_name,
        ", ".join(str(dna.get(k)) for k in ("hair", "eyes", "skin") if dna.get(k)),
        str(wardrobe.spec.get("description", "")) if wardrobe is not None else "",
        f"{state.get('expression', 'neutral')} expression, {str(state.get('posture', '')).replace('_', ' ')}",
        f"{shot.camera.framing} shot, {shot.camera.angle}",
        *(profile.prompt_hints if profile is not None else []),
        shot.visual.prompt_extra if shot.visual else "",
    ]
    base = plate.ref("image") if plate is not None else (references[0] if references else None)
    if base is None:
        raise ValueError(f"{run.node.key}: no world plate and no canonical face to compose on")
    request = m.ImageEditRequest(
        image=base,
        prompt=", ".join(p for p in prompt_parts if p),
        negative=shot.visual.negative if shot.visual else "",
        references=references,
        width=int(run.node.params["width"]),
        height=int(run.node.params["height"]),
        labels={
            "kind": "keyframe",
            "creator": creator.display_name,
            "shot": shot.key,
            "expression": str(state.get("expression", "")),
        },
    )
    return Prepared(request)


async def _shot_audio_artifact(run: NodeRun, window: tuple[float, float] | None = None) -> tuple[ArtifactRef, Any]:
    audio = run.shot_audio()
    pieces = []
    for piece in audio.pieces:
        path = await run.fetch(run.tts_audio(piece.segment_key))
        pieces.append((path, piece.from_s, piece.to_s, piece.at_s))
    w0, w1 = window or (0.0, audio.duration_s)
    shifted = [(p, a, b, at - w0) for p, a, b, at in pieces]
    out = await assemble(shifted, w1 - w0, run.scratch / f"shot_audio_{w0:.3f}.wav")
    return await run.write(out, "audio", role="shot_audio", mime="audio/wav"), audio


async def _avatar(run: NodeRun) -> Prepared:
    shot = run.shot
    params = run.node.params
    chunk, chunks = int(params["chunk"]), int(params["chunks"])
    words = run.shot_word_list()
    audio = run.shot_audio()
    counts = _chunk_counts(run, words)
    windows = chunk_windows(audio, counts)
    window = windows[chunk - 1]
    chunk_audio, _ = await _shot_audio_artifact(run, window)
    compiled = run.dep("behavior.compile_visual:")
    keyframe = run.dep("image.keyframe:").ref("image")
    member = next(c for c in run.spec.cast if c.key == shot.character_key)
    creator = run.data.refs.creator(member.creator_version_id)
    request = m.AvatarRequest(
        keyframe=keyframe,
        audio=chunk_audio,
        behavior=BehaviorDirectives.model_validate(compiled.data["directives"]),
        width=int(params["width"]),
        height=int(params["height"]),
        fps=float(params["fps"]),
        chunk_index=chunk,
        labels={
            "shot": shot.key,
            "take": str(run.node.take or 1),
            "chunk": f"{chunk}/{chunks}",
            "creator": creator.display_name,
        },
    )
    return Prepared(request, extra={"window": list(window), "shot_words": [list(w) for w in audio.words]})


def _chunk_counts(run: NodeRun, words: list[tuple[str, int]]) -> list[int]:
    """Word counts per chunk from the compile nodes' recorded ranges (`params.range`)."""
    ranges = sorted(
        (int(n.params["chunk"]), tuple(n.params["range"][0]), tuple(n.params["range"][1]))
        for n in run.graph.nodes
        if n.kind == "behavior.compile_visual" and n.shot_key == run.node.shot_key
    )
    position = {w: i for i, w in enumerate(words)}
    return [position[(e[0], e[1])] - position[(s[0], s[1])] + 1 for _, s, e in ranges]


def select_take(run: NodeRun) -> tuple[int, dict[str, float]]:
    """The take to use: the spec's `selected_take_key`, else the best `qc.shot` score (lowest index
    on ties). Scores are keyed by the take index as text (documents are canonical JSON)."""
    numeric: dict[int, float] = {}
    for key, output in run.deps("qc.shot:"):
        numeric[int(key.rsplit(":t", 1)[1])] = float(output.data.get("score", 0.0))
    scores = {str(k): v for k, v in sorted(numeric.items())}
    takes = sorted(
        {int(k.rsplit(":t", 1)[1]) for k in run.node.deps if k.startswith(("avatar.render:", "video.broll:"))}
    )
    selected_key = run.shot.takes.selected_take_key
    if selected_key:
        index = int(selected_key.rsplit("_", 1)[1])
        if index in takes:
            return index, scores
    if not takes:
        raise LookupError(f"{run.node.key} has no takes")
    best = max(takes, key=lambda t: (numeric.get(t, 0.0), -t))
    return best, scores


async def take_video(run: NodeRun, take: int) -> ArtifactRef:
    """The take's video: its single output, or its chunks concatenated."""
    chunks = sorted(
        (int(k.split(":c")[1].split(":")[0]) if ":c" in k else 1, out)
        for k, out in run.deps("avatar.render:")
        if k.endswith(f":t{take}")
    )
    if not chunks:
        broll = [out for k, out in run.deps("video.broll:") if k.endswith(f":t{take}")]
        return broll[0].ref("video")
    if len(chunks) == 1:
        return chunks[0][1].ref("video")
    paths = [await run.fetch(out.ref("video")) for _, out in chunks]
    joined = await concat(paths, run.scratch / f"take_{take}.mp4")
    return await run.write(joined, "video", role="take", mime="video/mp4")


def _timing(run: NodeRun, take: int) -> dict[str, Any]:
    """Shot timing that travels with the clip down to `render.final` (from the take's chunk outputs)."""
    outs = [o for k, o in run.deps("avatar.render:") if k.endswith(f":t{take}")]
    pad = run.svc.bundle.app.render.timeline.shot_pad_in_s
    return {
        "span_offset_s": pad,
        "shot_words": outs[0].data.get("shot_words", []),
        "clip_duration_s": max(float(o.data["window"][1]) for o in outs),
    }


async def _expression(run: NodeRun) -> Prepared:
    take, scores = select_take(run)
    patch = run.maybe("lipsync.patch:")
    video = patch.ref("video") if patch is not None else await take_video(run, take)
    request = m.ExpressionEditRequest(video=video, operations=[], labels={"shot": run.shot.key, "take": str(take)})
    return Prepared(request, extra={"selected_take": take, "scores": scores, **_timing(run, take)})


async def _lipsync(run: NodeRun) -> Prepared:
    take, _ = select_take(run)
    video = await take_video(run, take)
    audio, _ = await _shot_audio_artifact(run)
    return Prepared(
        m.LipSyncRequest(video=video, audio=audio, labels={"shot": run.shot.key}), extra={"selected_take": take}
    )


def _broll_timing(run: NodeRun) -> tuple[float, list[list[float]]]:
    shot = run.shot
    if shot.span.kind == "duration":
        return shot.span.duration_ms / 1000.0, []  # type: ignore[union-attr]
    audio = run.shot_audio(pad=False)
    return audio.duration_s, [list(w) for w in audio.words]


async def _broll(run: NodeRun) -> Prepared:
    shot = run.shot
    broll = shot.broll
    assert broll is not None
    duration, words = _broll_timing(run)
    profile = run.svc.bundle.camera_profiles.get(shot.camera.profile_id)
    plate = run.maybe("world.plate:")
    prompt = ", ".join(p for p in [broll.prompt, *(profile.prompt_hints if profile else [])] if p)
    request = m.VideoGenerateRequest(
        prompt=prompt,
        negative="text, watermark" if not broll.allow_text_in_frame else "",
        first_frame=plate.ref("image") if plate is not None and run.node.capability == "video.i2v" else None,
        duration_s=max(0.5, round(duration, 3)),
        fps=float(run.node.params["fps"]),
        width=int(run.node.params["width"]),
        height=int(run.node.params["height"]),
        labels={"shot": shot.key, "take": str(run.node.take or 1), "kind": str(shot.type)},
    )
    return Prepared(request, extra={"span_offset_s": 0.0, "shot_words": words, "clip_duration_s": duration})


async def _source_video(run: NodeRun) -> tuple[ArtifactRef, dict[str, Any]]:
    """The video an upscale/interpolate node processes, with the timing data it carries."""
    for prefix in ("video.upscale:", "post.expression:", "lipsync.patch:"):
        out = run.maybe(prefix)
        if out is not None:
            return out.ref("video"), dict(out.data)
    take, scores = select_take(run)
    video = await take_video(run, take)
    broll = [o for k, o in run.deps("video.broll:") if k.endswith(f":t{take}")]
    data = dict(broll[0].data) if broll else {}
    return video, {**data, "selected_take": take, "scores": scores}


async def _upscale(run: NodeRun) -> Prepared:
    video, data = await _source_video(run)
    request = m.UpscaleRequest(
        video=video,
        target_width=int(run.node.params["target_width"]),
        target_height=int(run.node.params["target_height"]),
        labels={"shot": run.node.shot_key or ""},
    )
    return Prepared(request, extra=data)


async def _interpolate(run: NodeRun) -> Prepared:
    video, data = await _source_video(run)
    request = m.InterpolateRequest(
        video=video, target_fps=float(run.node.params["target_fps"]), labels={"shot": run.node.shot_key or ""}
    )
    return Prepared(request, extra=data)


def _music_duration(run: NodeRun) -> float:
    cfg = run.svc.bundle.app.render.timeline
    segments = run.segment_audio()
    total = sum(s.duration_s for s in segments.values()) + cfg.segment_gap_s * max(0, len(segments) - 1)
    return round(total + cfg.lead_s + cfg.tail_s + 1.0, 3)


async def _music(run: NodeRun) -> Prepared:
    key = run.node.key.split(":", 1)[1]
    cue = next(c for c in run.spec.audio.music.cues if c.key == key)
    request = m.MusicRequest(
        description=cue.mood or "light instrumental bed",
        duration_s=_music_duration(run),
        bpm=cue.bpm,
        labels={"cue": key},
    )
    return Prepared(request)


async def _sfx(run: NodeRun) -> Prepared:
    key = run.node.key.split(":", 1)[1]
    event = next(s for s in run.spec.audio.sfx if s.key == key)
    request = m.SfxRequest(
        description=event.description, duration_s=float(run.node.params.get("duration_s", 1.0)), labels={"sfx": key}
    )
    return Prepared(request)


async def _translate(run: NodeRun) -> Prepared:
    language = run.node.key.split(":", 1)[1]
    captions = run.dep("captions.build:")
    source = run.spec.captions.language or run.spec.meta.language
    return Prepared(
        m.CaptionTranslateRequest(captions=captions.ref("ass"), source_language=source, target_language=language)
    )


BUILDERS: dict[str, Callable[[NodeRun], Awaitable[Prepared]]] = {
    "voice.prepare": _voice_prepare,
    "tts.segment": _tts,
    "asr.verify": _asr_verify,
    "align.segment": _align,
    "world.plate": _world_plate,
    "image.keyframe": _keyframe,
    "avatar.render": _avatar,
    "post.expression": _expression,
    "lipsync.patch": _lipsync,
    "video.broll": _broll,
    "video.upscale": _upscale,
    "video.interpolate": _interpolate,
    "audio.music": _music,
    "audio.sfx": _sfx,
    "captions.translate": _translate,
}


async def build_request(run: NodeRun) -> Prepared:
    builder = BUILDERS.get(run.node.kind)
    if builder is None:
        raise LookupError(f"no request builder for {run.node.kind}")
    return await builder(run)
