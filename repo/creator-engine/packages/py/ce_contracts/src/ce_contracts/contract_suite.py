"""The adapter contract suite (§37): shared checks every adapter must pass.

`check_adapter(plugin, workdir)` runs, for one loaded plugin:

1. the manifest is valid and the adapter satisfies the `Adapter` protocol;
2. `load`, `health`, `estimate` and `unload` work;
3. every declared capability runs on a small sample request and returns its result model;
   every `ArtifactRef` in the result exists in the context with a matching hash;
4. translator conformance for behavior-capable plugins: the translator keeps the request type,
   fills `engine`, never removes directives, every item it claims to encode is a directive item,
   and every directive is either encoded or reported in `engine.unsupported` (never dropped).

Sample inputs are generated with FFmpeg (lavfi), so the suite needs no fixtures. Interface-only
capabilities run like the others when a plugin declares them. Real CPU engines run for real on the
fetched assets (`make fetch-cpu-assets`); this is their smoke test (`validation: smoke_passed`).
Real GPU adapters run the suite with heavy calls mocked (Phase 8): their manifest names a
`test_backend`, a CPU stand-in for the model's forward pass that the suite swaps in through the
adapter's `use_backend()`; everything else in the adapter runs for real. `real_backends=True`
(a GPU host with the weights) runs the real backends instead.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ce_contracts import models as m
from ce_contracts.behavior import (
    BehaviorDirectives,
    DirectiveRealization,
    DirectiveSubSpan,
    ProsodyDirectives,
    VisualDirectives,
)
from ce_contracts.capabilities import CAPABILITIES
from ce_contracts.common import ArtifactRef, Estimate, HardwareInfo, HealthStatus, LoadContext
from ce_contracts.interfaces import Adapter, BehaviorTranslator
from ce_contracts.local import LocalRunContext, file_sha256
from ce_contracts.plugins import LoadedPlugin, import_object

__all__ = ["SampleInputs", "check_adapter", "sample_directives", "sample_request", "uses_test_backend"]


def _ffmpeg(*args: str) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("the adapter contract suite needs ffmpeg")
    subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)  # noqa: S603


class SampleInputs:
    """Tiny real media files registered in a LocalRunContext."""

    def __init__(self, ctx: LocalRunContext) -> None:
        self.ctx = ctx
        self.refs: dict[str, ArtifactRef] = {}

    async def build(self) -> SampleInputs:
        root = self.ctx.root / "samples"
        root.mkdir(parents=True, exist_ok=True)
        png, wav, mp4, txt, ass = (root / n for n in ("still.png", "tone.wav", "clip.mp4", "doc.txt", "captions.ass"))
        _ffmpeg("-f", "lavfi", "-i", "color=c=0x406080:s=64x96", "-frames:v", "1", str(png))
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=1.2:sample_rate=48000",
            "-c:a",
            "pcm_s16le",
            "-ac",
            "1",
            str(wav),
        )
        _ffmpeg(
            "-f", "lavfi", "-i", "testsrc2=s=64x96:r=12:d=1.2", "-f", "lavfi", "-i", "sine=frequency=330:duration=1.2",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(mp4),
        )  # fmt: skip
        txt.write_text("A short document for ingestion.\n", encoding="utf-8")
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\nDialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Hello there\n",
            encoding="utf-8",
        )
        for name, path, kind in (
            ("png", png, "image"),
            ("wav", wav, "audio"),
            ("mp4", mp4, "video"),
            ("txt", txt, "other"),
            ("ass", ass, "captions"),
        ):
            self.refs[name] = await self.ctx.put_file(path, kind)
        return self


def sample_directives() -> BehaviorDirectives:
    """Directives covering a state emotion (segment), a gaze event (word) and an unsupported gesture."""
    return BehaviorDirectives(
        cbs_content_digest="sha256:" + "0" * 64,
        target_key="sht_test",
        realizations=[
            DirectiveRealization(
                item_ref="/scenes[scn_t]/acting/states[st_1]/emotion",
                dimension="emotion_visual",
                level="HONORED",
                method="native_segment",
            ),
            DirectiveRealization(
                item_ref="/scenes[scn_t]/acting/states[st_1]/emotion",
                dimension="emotion_visual",
                level="APPROXIMATED",
                method="text_prompt_global",
            ),
            DirectiveRealization(
                item_ref="/scenes[scn_t]/acting/events[ev_1]",
                dimension="gaze",
                level="HONORED",
                method="native_parametric",
            ),
            DirectiveRealization(
                item_ref="/scenes[scn_t]/acting/states[st_1]/strategies/gesture",
                dimension="gesture",
                level="UNSUPPORTED",
                method="omit",
            ),
        ],
        visual=[
            VisualDirectives(
                shot_key="sht_test",
                character_key="char_test",
                sub_spans=[
                    DirectiveSubSpan(
                        start_s=0.0,
                        end_s=1.0,
                        labels={"emotion_visual": "serious@0.6"},
                        item_refs=["/scenes[scn_t]/acting/states[st_1]/emotion"],
                    ),
                    DirectiveSubSpan(
                        start_s=0.4,
                        end_s=0.8,
                        labels={"gaze": "look_away:down_left"},
                        item_refs=["/scenes[scn_t]/acting/events[ev_1]"],
                    ),
                ],
            )
        ],
        prosody=[
            ProsodyDirectives(
                character_key="char_test",
                segment_key="seg_t",
                strategy="slow_measured",
                rate=0.9,
                pauses=[{"after_word": 1, "ms": 300}],
                emphasis_words=[2],
            )
        ],
    )


def sample_request(capability: str, inputs: SampleInputs) -> BaseModel:
    r = inputs.refs
    size: dict[str, Any] = {"width": 64, "height": 96}
    builders: dict[str, Callable[[], BaseModel]] = {
        "llm.structured": lambda: m.LLMRequest(
            messages=[m.LLMMessage(role="user", content="hi")], json_schema={"type": "object"}
        ),
        "embed.text": lambda: m.TextEmbedRequest(texts=["a test sentence", "another one"]),
        "research.fetch": lambda: m.ResearchFetchRequest(url="https://example.test/page"),
        "research.ingest": lambda: m.ResearchIngestRequest(document=r["txt"], chunk_chars=10),
        "research.search": lambda: m.ResearchSearchRequest(query="agents"),
        "image.generate": lambda: m.ImageGenerateRequest(prompt="a portrait", labels={"creator": "Test"}, **size),
        "image.edit": lambda: m.ImageEditRequest(image=r["png"], prompt="compose", references=[r["png"]], **size),
        "avatar.a2v": lambda: m.AvatarRequest(
            keyframe=r["png"],
            audio=r["wav"],
            behavior=sample_directives(),
            fps=12,
            labels={"shot": "sht_test", "take": "1"},
            **size,
        ),
        "voice.tts": lambda: m.TTSRequest(
            text="Hello there, my friend.",
            words=["Hello", "there,", "my", "friend."],
            language="en",
            behavior=sample_directives(),
        ),
        "voice.design": lambda: m.VoiceDesignRequest(
            description="warm voice", language="en", sample_text="Testing one two.", count=2
        ),
        "voice.clone_prepare": lambda: m.VoicePrepareRequest(
            references=[m.VoiceReference(audio=r["wav"], transcript="tone", language="en")]
        ),
        "voice.convert": lambda: m.VoiceConvertRequest(audio=r["wav"], target=m.VoiceConditioning()),
        "lipsync.dub": lambda: m.LipSyncRequest(video=r["mp4"], audio=r["wav"]),
        "asr.transcribe": lambda: m.TranscribeRequest(audio=r["wav"], language="en", hint_text="tone"),
        "asr.align": lambda: m.AlignRequest(
            audio=r["wav"], text="a pure tone", words=["a", "pure", "tone"], language="en"
        ),
        "asr.lid": lambda: m.LidRequest(audio=r["wav"]),
        "audio.music": lambda: m.MusicRequest(description="light", duration_s=1.0, bpm=100),
        "audio.sfx": lambda: m.SfxRequest(description="soft whoosh", duration_s=0.5),
        "vision.image": lambda: m.VisionRequest(media=r["png"], question="is there a face?"),
        "vision.video": lambda: m.VisionRequest(
            media=r["mp4"], question="does the person look away?", window_s=(0.2, 0.8)
        ),
        "vision.ocr": lambda: m.OcrRequest(media=r["png"]),
        "face.detect": lambda: m.MediaAnalysisRequest(media=r["mp4"]),
        "face.landmarks": lambda: m.MediaAnalysisRequest(media=r["mp4"]),
        "face.embed": lambda: m.MediaAnalysisRequest(media=r["png"]),
        "body.landmarks": lambda: m.MediaAnalysisRequest(media=r["mp4"]),
        "audio.prosody": lambda: m.AudioAnalysisRequest(audio=r["wav"]),
        "audio.emotion": lambda: m.AudioAnalysisRequest(audio=r["wav"]),
        "voice.embed": lambda: m.AudioAnalysisRequest(audio=r["wav"]),
        "image.embed": lambda: m.ImageEmbedRequest(images=[r["png"]]),
        "video.upscale": lambda: m.UpscaleRequest(video=r["mp4"], target_width=128, target_height=192),
        "video.interpolate": lambda: m.InterpolateRequest(video=r["mp4"], target_fps=24),
        "qc.lipsync": lambda: m.QCMetricRequest(media=r["mp4"]),
        "qc.vqa": lambda: m.QCMetricRequest(media=r["mp4"]),
        "qc.speech_quality": lambda: m.QCMetricRequest(media=r["wav"]),
        "captions.build": lambda: m.CaptionBuildRequest(
            words=[
                m.CaptionWord(text=w, start_s=i * 0.3, end_s=i * 0.3 + 0.25)
                for i, w in enumerate(["Hello", "there,", "friend."])
            ],
            language="en",
            style={
                "font_family": "DejaVu Sans",
                "font_size_px": 64,
                "primary_color": "#FFFFFF",
                "highlight_color": "#FFD400",
                "stroke_color": "#000000",
                "stroke_width_px": 4,
            },
            width=1080,
            height=1920,
        ),
        "captions.translate": lambda: m.CaptionTranslateRequest(
            captions=r["ass"], source_language="en", target_language="de"
        ),
        "effects.transition": lambda: m.EffectRequest(effect={}, duration_s=0.5, fps=12, text="cut", **size),
        "effects.title": lambda: m.EffectRequest(effect={}, duration_s=0.5, fps=12, text="Title", **size),
        "effects.overlay": lambda: m.EffectRequest(effect={}, duration_s=0.5, fps=12, text="Lower third", **size),
        "effects.disclosure": lambda: m.EffectRequest(effect={}, duration_s=0.5, fps=12, text="Dramatization", **size),
        "expression.edit": lambda: m.ExpressionEditRequest(video=r["mp4"], operations=[{"op": "blink", "at_s": 0.5}]),
        "identity.train": lambda: m.IdentityTrainRequest(
            appearance_version_id="test", dataset=[r["png"]], base_model="base"
        ),
        "provenance.watermark_video": lambda: m.WatermarkRequest(media=r["mp4"], payload_id="p1"),
        "provenance.watermark_audio": lambda: m.WatermarkRequest(media=r["wav"], payload_id="p1"),
        "provenance.sign": lambda: m.SignRequest(media=r["mp4"], manifest={"assertions": []}),
        "provenance.verify": lambda: m.VerifyRequest(media=r["mp4"], layer="c2pa"),
    }
    for capability_id in ("video.t2v", "video.r2v", "video.edit", "video.extend", "video.joint_av"):
        builders[capability_id] = lambda: m.VideoGenerateRequest(prompt="b-roll", duration_s=0.8, fps=12, **size)
    builders["video.i2v"] = lambda: m.VideoGenerateRequest(
        prompt="b-roll", first_frame=r["png"], duration_s=0.8, fps=12, **size
    )
    return builders[capability]()


def _refs(value: Any) -> list[ArtifactRef]:
    if isinstance(value, ArtifactRef):
        return [value]
    if isinstance(value, BaseModel):
        return [ref for field in value.__class__.model_fields for ref in _refs(getattr(value, field))]
    if isinstance(value, list | tuple):
        return [ref for item in value for ref in _refs(item)]
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _refs(item)]
    return []


async def _check_translator(plugin: LoadedPlugin, translator: BehaviorTranslator, inputs: SampleInputs) -> None:
    directives = sample_directives()
    for decl in plugin.manifest.capabilities:
        if not CAPABILITIES[decl.id].behavior_capable:
            continue
        base = sample_request(decl.id, inputs).model_copy(update={"engine": {}})
        out = translator.translate(directives, base)
        assert type(out) is type(base), f"{plugin.id}: translator changed the request type"
        assert out.engine, f"{plugin.id}: translator produced no engine request"  # type: ignore[attr-defined]
        assert out.behavior == base.behavior, f"{plugin.id}: translator altered the directives"  # type: ignore[attr-defined]
        directive_items = {(r.item_ref, r.dimension) for r in directives.realizations}
        engine = out.engine  # type: ignore[attr-defined]
        claimed = {(e["item_ref"], e["dimension"]) for e in engine.get("encoded", [])}
        reported = {(e["item_ref"], e["dimension"]) for e in engine.get("unsupported", [])}
        assert claimed <= directive_items, f"{plugin.id}: translator encodes items that were never requested"
        missing = directive_items - claimed - reported
        assert not missing, f"{plugin.id}: directives neither translated nor reported unsupported: {sorted(missing)}"


def check_adapter(
    plugin: LoadedPlugin, workdir: Path, *, model_cache_dir: Path | None = None, real_backends: bool = False
) -> None:
    """Runs the contract for one plugin (synchronously; use from a plain pytest test). Real CPU
    engines need `model_cache_dir` with their assets (callers skip when they are missing). GPU
    engines run on their manifest's `test_backend` unless `real_backends` is set."""
    asyncio.run(_check(plugin, workdir, model_cache_dir, real_backends=real_backends))


def uses_test_backend(plugin: LoadedPlugin, *, real_backends: bool = False) -> bool:
    return plugin.manifest.test_backend is not None and not real_backends


async def _check(
    plugin: LoadedPlugin, workdir: Path, model_cache_dir: Path | None = None, *, real_backends: bool = False
) -> None:
    adapter = plugin.adapter()
    assert isinstance(adapter, Adapter), f"{plugin.id}: not an Adapter"
    if uses_test_backend(plugin, real_backends=real_backends):
        factory = import_object(str(plugin.manifest.test_backend))
        use_backend = getattr(adapter, "use_backend", None)
        assert callable(use_backend), f"{plugin.id}: declares test_backend but its adapter has no use_backend()"
        use_backend(factory(plugin.manifest))
    ctx = LocalRunContext(workdir, seed=4242)
    inputs = await SampleInputs(ctx).build()
    await adapter.load(
        LoadContext(
            model_cache_dir=str(model_cache_dir or workdir / "models"), scratch_dir=str(ctx.scratch_dir), app_env="test"
        )
    )
    health = await adapter.health()
    assert isinstance(health, HealthStatus) and health.ok, f"{plugin.id}: unhealthy after load"
    translator = plugin.translator()
    if translator is not None:
        assert isinstance(translator.version, str) and translator.version
        await _check_translator(plugin, translator, inputs)
    for decl in plugin.manifest.capabilities:
        request = sample_request(decl.id, inputs)
        estimate = adapter.estimate(request, HardwareInfo())
        assert isinstance(estimate, Estimate)
        run: Callable[..., Awaitable[BaseModel]] = adapter.run
        result = await run(decl.id, request, ctx)
        assert isinstance(result, CAPABILITIES[decl.id].result), (
            f"{plugin.id}.{decl.id} returned {type(result).__name__}"
        )
        for ref in _refs(result):
            path = await ctx.read_artifact(ref)
            assert file_sha256(path) == ref.sha256
            assert ref.kind, f"{plugin.id}.{decl.id}: artifact without kind"
    await adapter.unload()
