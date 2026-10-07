"""Every build-graph node kind of §12.1 with where it runs and what it reads.

`executor` is the default; a node may switch to `cpu` when it only reuses an existing asset (an
approved canonical plate, a user-supplied keyframe or B-roll asset). `impl_version` must be bumped
whenever deterministic code changes a node's output (§12.2). Kinds marked `stub` are mock-backed or
pass-through implementations until the phase named in `stub`.

`spec_reads` declares, as SpecPath patterns, which spec fields a node reads; dirty analysis and
the edit vocabulary use these declarations (§12.1). The graph builder hashes exactly these
fragments into the node's spec digest.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["KINDS", "NodeKind", "kind"]


@dataclass(frozen=True)
class NodeKind:
    kind: str
    executor: Literal["cpu", "model", "render"]
    capability: str | None
    impl_version: str
    group: Literal["scene", "video"]
    reads_requests: bool = False  # the CBS content digest enters the cache key (§12.2)
    spec_reads: tuple[str, ...] = ()
    stub: str | None = None  # "phase N": mock-backed or pass-through until that phase


_K = NodeKind
KINDS: dict[str, NodeKind] = {
    k.kind: k
    for k in (
        _K(
            "behavior.resolve",
            "cpu",
            None,
            "1",
            "scene",
            spec_reads=(
                "/scenes[*]/acting",
                "/scenes[*]/intent",
                "/scenes[*]/cast",
                "/script/segments[*]/annotations",
                "/scenes[*]/world/overrides",
                "/locks",
            ),
        ),
        _K(
            "behavior.compile_voice",
            "cpu",
            None,
            "2",
            "scene",
            reads_requests=True,
            spec_reads=("/script/segments[*]",),
        ),
        _K(
            "voice.prepare",
            "model",
            "voice.clone_prepare",
            "1",
            "video",
            spec_reads=("/cast[*]/overrides/voice_version_id",),
        ),
        _K(
            "tts.segment",
            "model",
            "voice.tts",
            "2",
            "scene",
            spec_reads=("/script/segments[*]", "/cast[*]/voice_prosody", "/meta/language"),
        ),
        _K("asr.verify", "model", "asr.transcribe", "3", "scene", spec_reads=("/script/segments[*]/text",)),
        _K(
            "align.segment",
            "model",
            "asr.align",
            "3",
            "scene",
            spec_reads=("/script/segments[*]/text", "/tokenizer_version"),
        ),
        _K(
            "behavior.compile_visual",
            "cpu",
            None,
            "2",
            "scene",
            reads_requests=True,
            spec_reads=("/scenes[*]/shots[*]", "/audio/sfx", "/audio/music", "/captions", "/meta/mode"),
        ),
        _K("world.plate", "model", "image.edit", "1", "scene", spec_reads=("/scenes[*]/world",)),
        _K(
            "behavior.keyframe_state",
            "cpu",
            None,
            "2",
            "scene",
            reads_requests=True,
            spec_reads=("/scenes[*]/cast", "/scenes[*]/shots[*]/span"),
        ),
        _K(
            "image.keyframe",
            "model",
            "image.edit",
            "1",
            "scene",
            spec_reads=(
                "/scenes[*]/shots[*]/camera",
                "/scenes[*]/shots[*]/visual",
                "/scenes[*]/cast[*]/wardrobe_version_id",
            ),
        ),
        _K("avatar.render", "model", "avatar.a2v", "1", "scene", spec_reads=("/scenes[*]/shots[*]/takes",)),
        _K("lipsync.patch", "model", "lipsync.dub", "1", "scene"),
        _K(
            "post.expression",
            "model",
            "expression.edit",
            "1",
            "scene",
            spec_reads=("/scenes[*]/shots[*]/takes/selected_take_key",),
            stub="V2 (pass-through)",
        ),
        _K(
            "video.broll",
            "model",
            "video.t2v",
            "1",
            "scene",
            spec_reads=("/scenes[*]/shots[*]/broll", "/scenes[*]/shots[*]/takes"),
        ),
        _K("video.upscale", "model", "video.upscale", "1", "scene"),
        _K("video.interpolate", "model", "video.interpolate", "1", "scene"),
        _K("screen.prepare", "cpu", None, "1", "scene", spec_reads=("/scenes[*]/shots[*]/screen",)),
        _K("behavior.observe", "cpu", "face.landmarks", "2", "scene"),
        _K(
            "qc.shot",
            "cpu",
            None,
            "3",
            "scene",
            reads_requests=True,
        ),
        _K(
            "qc.world",
            "cpu",
            None,
            "2",
            "scene",
            spec_reads=("/scenes[*]/world",),
        ),
        _K(
            "post.camera",
            "render",
            None,
            "4",
            "scene",
            spec_reads=("/scenes[*]/shots[*]/camera", "/meta/primary_aspect", "/render/reframe"),
        ),
        _K("post.realism", "render", None, "1", "scene"),
        _K("audio.music", "model", "audio.music", "1", "video", spec_reads=("/audio/music",)),
        _K("audio.sfx", "model", "audio.sfx", "1", "video", spec_reads=("/audio/sfx",)),
        _K(
            "audio.room",
            "render",
            None,
            "1",
            "scene",
            spec_reads=("/audio/acoustics", "/scenes[*]/world"),
        ),
        _K("captions.build", "render", "captions.build", "2", "video", spec_reads=("/captions", "/script/segments[*]")),
        _K("captions.translate", "model", "captions.translate", "1", "video", spec_reads=("/captions/translations",)),
        _K("mix.audio", "render", None, "3", "video", spec_reads=("/audio",)),
        _K(
            "render.final",
            "render",
            None,
            "3",
            "video",
            spec_reads=("/render", "/scenes[*]/shots[*]", "/provenance", "/captions"),
        ),
        _K("render.proxy", "render", None, "1", "video"),
        _K("provenance.watermark_video", "render", "provenance.watermark_video", "1", "video"),
        _K("provenance.watermark_audio", "render", "provenance.watermark_audio", "1", "video"),
        _K("provenance.sign", "render", "provenance.sign", "2", "video", spec_reads=("/provenance",)),
        _K("qc.render", "render", None, "2", "video"),
        _K("behavior.coverage", "cpu", None, "2", "video", reads_requests=True),
        _K("qc.continuity", "cpu", None, "1", "video"),
    )
}


def kind(name: str) -> NodeKind:
    try:
        return KINDS[name]
    except KeyError:
        raise KeyError(f"unknown node kind {name!r}") from None
