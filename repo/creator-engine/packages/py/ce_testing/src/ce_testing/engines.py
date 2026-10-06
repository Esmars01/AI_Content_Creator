"""Shared fixtures for real-engine plugin tests (Phase 8): directives compiled for an engine's
declared matrix, a single-state clip, golden-file comparison and small real media inputs.

Nothing here runs a model: engines run on their CPU stand-ins (`test_backend`), so these tests
prove the adapter code around the engine, never the engine itself (rule 5)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ce_behavior.directives import to_directives
from ce_behavior.scene import SceneWords
from ce_contracts.behavior import (
    BehaviorDirectives,
    DirectiveRealization,
    DirectiveSubSpan,
    ProsodyDirectives,
    VisualDirectives,
)
from ce_contracts.common import ArtifactRef, LoadContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import LoadedPlugin

from ce_testing.behavior import bundle, real_engine_catalog, version_behavior

__all__ = [
    "PLACEHOLDER",
    "assert_golden",
    "assert_translation_complete",
    "example_directives",
    "load_with_backend",
    "make_media",
    "single_state_directives",
    "voice_directives",
]

PLACEHOLDER = ArtifactRef(sha256="0" * 64, kind="image", mime="image/png")


def _times(words: SceneWords) -> dict[tuple[str, int], tuple[float, float]]:
    return {w: (round(0.1 + 0.33 * i, 3), round(0.38 + 0.33 * i, 3)) for i, w in enumerate(words.order)}


def example_directives(adapter_id: str, target: str) -> BehaviorDirectives:
    """The §11 example version compiled for `adapter_id`'s matrix (the real compiler), as the wire
    directives of `target` (`sht_1:c1` for the talking shot, `seg_1`/`seg_2` for voice)."""
    vb = version_behavior(real_engine_catalog({adapter_id}))
    words = SceneWords.of(vb.spec, vb.spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == target)
    times = _times(words) if target.startswith("sht_") else {}
    return to_directives(compiled, vb.cbs["scn_hook"], words=words, word_times=times, vocab=bundle().vocab)


def single_state_directives() -> BehaviorDirectives:
    """A clip covered by one acting state: emotion, posture, gesture and camera awareness compile to
    `text_prompt_global` (the example's talking shot spans two states)."""
    vocab = bundle().vocab
    base = "/scenes[scn_a]/acting/states[st_1]"
    items = [
        (f"{base}/emotion", "emotion_visual", "confident@0.9", vocab.describe("emotion", "confident")),
        (
            f"{base}/strategies/posture",
            "posture",
            "seated_lean_in",
            vocab.describe("strategy.posture", "seated_lean_in"),
        ),
        (
            f"{base}/strategies/gesture",
            "gesture",
            "illustrative_light",
            vocab.describe("strategy.gesture", "illustrative_light"),
        ),
        (
            f"{base}/strategies/camera_awareness",
            "camera_awareness",
            "direct_address",
            vocab.describe("strategy.camera_awareness", "direct_address"),
        ),
    ]
    return BehaviorDirectives(
        cbs_content_digest="sha256:" + "2" * 64,
        target_key="sht_a:c1",
        realizations=[
            *(
                DirectiveRealization(item_ref=r, dimension=d, level="APPROXIMATED", method="text_prompt_global")
                for r, d, _, _ in items
            ),
            DirectiveRealization(
                item_ref=f"{base}/strategies/gaze", dimension="gaze", level="UNSUPPORTED", method="omit"
            ),
        ],
        visual=[
            VisualDirectives(
                shot_key="sht_a",
                character_key="char_a",
                sub_spans=[
                    DirectiveSubSpan(
                        start_s=0.0,
                        end_s=4.0,
                        item_refs=[r for r, _, _, _ in items],
                        labels={d: label for _, d, label, _ in items},
                        descriptions={d: str(getattr(t, "text", t)) for _, d, _, t in items if t is not None},
                    )
                ],
            )
        ],
    )


def voice_directives(
    *,
    emotion: str = "serious",
    strategy: str = "slow_measured",
    delivery: str | None = None,
    nonverbal: list[dict[str, Any]] | None = None,
    pauses: list[dict[str, int]] | None = None,
    rate: float = 0.9,
) -> BehaviorDirectives:
    """One segment's ProsodyPlan with the realizations a voice translator must handle."""
    vocab = bundle().vocab
    item = "/scenes[scn_a]/acting/states[st_1]"
    descriptions = {
        k: str(getattr(t, "vocal", None) or getattr(t, "text", t))
        for k, t in (
            ("emotion", vocab.describe("emotion", emotion)),
            ("strategy", vocab.describe("strategy.prosody", strategy)),
            ("delivery", vocab.describe("annotation_tag.delivery", delivery) if delivery else None),
        )
        if t is not None
    }
    realizations = [
        DirectiveRealization(
            item_ref=f"{item}/emotion", dimension="emotion_vocal", level="APPROXIMATED", method="text_prompt_segment"
        ),
        DirectiveRealization(
            item_ref=f"{item}/strategies/prosody", dimension="prosody_rate", level="HONORED", method="native_parametric"
        ),
        DirectiveRealization(
            item_ref=f"{item}/strategies/prosody",
            dimension="prosody_energy",
            level="HONORED",
            method="native_parametric",
        ),
        DirectiveRealization(
            item_ref=f"{item}/strategies/prosody",
            dimension="prosody_pitch",
            level="HONORED",
            method="native_parametric",
        ),
        DirectiveRealization(
            item_ref="/script/segments[seg_a]/annotations[an_1]",
            dimension="prosody_pause",
            level="HONORED",
            method="native_parametric",
        ),
        DirectiveRealization(
            item_ref="/script/segments[seg_a]/annotations[an_2]",
            dimension="prosody_emphasis",
            level="APPROXIMATED",
            method="native_parametric",
        ),
    ]
    if nonverbal:
        realizations.append(
            DirectiveRealization(
                item_ref="/script/segments[seg_a]/annotations[an_3]",
                dimension="nonverbal_audio",
                level="HONORED",
                method="audio_nonverbal",
            )
        )
    if delivery:
        realizations.append(
            DirectiveRealization(
                item_ref="/script/segments[seg_a]/annotations[an_4]",
                dimension="delivery",
                level="APPROXIMATED",
                method="text_prompt_segment",
            )
        )
    realizations.append(
        DirectiveRealization(
            item_ref="/characters[char_a]/voice/accent", dimension="accent", level="UNSUPPORTED", method="omit"
        )
    )
    return BehaviorDirectives(
        cbs_content_digest="sha256:" + "3" * 64,
        target_key="seg_a",
        realizations=realizations,
        prosody=[
            ProsodyDirectives(
                character_key="char_a",
                segment_key="seg_a",
                strategy=strategy,
                emotion=emotion,
                emotion_intensity=0.7,
                rate=rate,
                energy=0.4,
                pitch_variation=0.3,
                emphasis_words=[2],
                pauses=pauses if pauses is not None else [{"after_word": 1, "ms": 300}],
                nonverbal=nonverbal or [],
                delivery=delivery,
                descriptions=descriptions,
            )
        ],
    )  # fmt: skip


def assert_golden(path: Path, produced: Any) -> None:
    """Compares JSON with a golden file; `CE_UPDATE_GOLDEN=1` rewrites it after an intended change."""
    data = json.loads(json.dumps(produced, sort_keys=True, default=str))
    if os.environ.get("CE_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    assert path.exists(), f"golden file missing: {path} (run with CE_UPDATE_GOLDEN=1)"
    assert data == json.loads(path.read_text(encoding="utf-8")), f"golden mismatch: {path.name}"


def assert_translation_complete(directives: BehaviorDirectives, engine: dict[str, Any]) -> None:
    """Every realization is encoded or reported (with a reason), never both, never dropped (§37)."""
    requested = {(r.item_ref, r.dimension) for r in directives.realizations}
    encoded = {(e["item_ref"], e["dimension"]) for e in engine["encoded"]}
    reported = {(e["item_ref"], e["dimension"]) for e in engine["unsupported"]}
    assert encoded | reported == requested, sorted(requested - encoded - reported)
    assert not encoded & reported, sorted(encoded & reported)
    assert all(e["reason"] for e in engine["unsupported"])


async def load_with_backend(plugin: LoadedPlugin, backend: Any, tmp: Path, **config: Any) -> Any:
    """A fresh adapter instance of `plugin` with `backend` swapped in, loaded in `tmp`."""
    adapter = plugin.adapter().__class__(plugin.manifest)
    adapter.use_backend(backend)
    await adapter.load(
        LoadContext(
            model_cache_dir=str(tmp / "models"), scratch_dir=str(tmp / "scratch"), app_env="test", config=config
        )
    )
    return adapter


async def make_media(ctx: LocalRunContext, tmp: Path, **kinds: str) -> dict[str, ArtifactRef]:
    """Small real inputs by name: `image` (360×640 PNG), `speech` (2 s WAV), `video` (1 s 360×640 MP4
    with audio), `tone48k` (1 s WAV)."""
    import shutil
    import subprocess

    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("make_media needs ffmpeg")

    def ffmpeg(*args: str) -> None:
        subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y", *args], check=True, timeout=120)  # noqa: S603

    sine = "sine=frequency={f}:duration={d}:sample_rate=48000"
    recipes = {
        "image": (["-f", "lavfi", "-i", "color=c=0x406080:s=360x640", "-frames:v", "1"], "png", "image"),
        "speech": (["-f", "lavfi", "-i", sine.format(f=200, d=2.0), "-ac", "1"], "wav", "audio"),
        "video": (
            [
                "-f", "lavfi", "-i", "testsrc2=s=360x640:r=25:d=1", "-f", "lavfi", "-i", sine.format(f=300, d=1),
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
            ],
            "mp4",
            "video",
        ),
        "tone48k": (["-f", "lavfi", "-i", sine.format(f=440, d=1.0), "-ac", "1"], "wav", "audio"),
    }  # fmt: skip
    out: dict[str, ArtifactRef] = {}
    for name, recipe in kinds.items():
        args, suffix, kind = recipes[recipe]
        path = tmp / f"{name}.{suffix}"
        ffmpeg(*args, str(path))
        out[name] = await ctx.put_file(path, kind)
    return out
