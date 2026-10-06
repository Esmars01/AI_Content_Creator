"""The two mock avatar engines (§16.7, §24).

Both loop the keyframe, overlay an audio-driven waveform at the mouth, burn the shot key, the
acting-state labels and the take number, and match the audio's duration. Both write
`behavior_track.json`: what they performed — the items their translator encoded (native, text,
keyframe-inherited or audio-coupled) — with seeded failure injection (`behavior_failure_rate`,
the environment variable `CE_MOCK_BEHAVIOR_FAIL_RATE`, or `labels["fail_items"]` for specific item
refs; approximated controls fail at least at `approximation_failure_rates`), plus emergent
behavior (blinks, small glances, head motion coupled to the audio).
"""

from __future__ import annotations

import json
from typing import Any, Literal

import numpy as np
from ce_contracts.common import RunContext
from ce_contracts.interfaces import AvatarEngine
from ce_contracts.models import AvatarRequest, AvatarResult

from ce_plugins_mock._base import MockAdapter, env_float
from ce_plugins_mock._media import probe_duration, read_wav, render_avatar_clip, seed_int
from ce_plugins_mock.avatar.translator import GlobalPromptTranslator, SegmentTranslator, parse_label
from ce_plugins_mock.behavior_track import BehaviorTrack, TrackItem

__all__ = ["MockAvatarGlobal", "MockAvatarSegment"]

# Encoded control → behavior-track source (how the engine performed it).
SOURCES: dict[str, Literal["native", "text", "keyframe", "audio_coupled"]] = {
    "native": "native",
    "text": "text",
    "keyframe": "keyframe",
    "audio": "audio_coupled",
}


class _MockAvatar(MockAdapter, AvatarEngine):
    seconds_per_unit = 0.6
    translator: Any = None

    def _failure_rate(self, request: AvatarRequest) -> float:
        if "behavior_failure_rate" in request.labels:
            return float(request.labels["behavior_failure_rate"])
        return env_float("CE_MOCK_BEHAVIOR_FAIL_RATE", float(self.config.get("behavior_failure_rate", 0.0)))

    def _approximation_rates(self) -> dict[str, float]:
        """Approximated controls fail more often than native ones (text, keyframe, audio coupling)."""
        return {k: float(v) for k, v in dict(self.config.get("approximation_failure_rates", {})).items()}

    def _coupling(self, control: str) -> float:
        """Audio-coupled expression shows at reduced intensity (the engine's prosody coupling)."""
        return float(self.config.get("audio_coupling_gain", 0.8)) if control == "audio" else 1.0

    def _emergent(self, duration: float, seed: int, energy: float, *, glances: bool = True) -> list[TrackItem]:
        rng = np.random.default_rng(seed)
        items: list[TrackItem] = []
        t = float(rng.uniform(0.8, 2.0))
        while t < duration - 0.2:
            items.append(
                TrackItem(
                    dimension="blink", label="blink", start_s=round(t, 3), end_s=round(t + 0.15, 3), source="emergent"
                )
            )
            t += float(rng.uniform(2.5, 4.5))
        if duration > 2.5 and rng.random() < 0.5 and glances:
            start = float(rng.uniform(0.5, duration - 1.0))
            items.append(
                TrackItem(
                    dimension="gaze",
                    label="glance",
                    start_s=round(start, 3),
                    end_s=round(start + 0.4, 3),
                    intensity=0.3,
                    source="emergent",
                )
            )
        items.append(
            TrackItem(
                dimension="head_motion",
                label="audio_coupled",
                start_s=0.0,
                end_s=round(duration, 3),
                intensity=float(np.clip(energy, 0, 1)),
                source="emergent",
            )
        )
        return items

    def _audio_energy(self, path: Any) -> float:
        try:
            samples, _ = read_wav(path)
        except Exception:  # not a 16-bit WAV: no coupling estimate
            return 0.5
        rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
        return float(np.clip(rms * 6.0, 0.0, 1.0))

    async def run_avatar_a2v(self, request: AvatarRequest, ctx: RunContext) -> AvatarResult:
        if request.behavior is not None and not request.engine and self.translator is not None:
            request = self.translator.translate(request.behavior, request)  # type: ignore[assignment]
        keyframe = await ctx.read_artifact(request.keyframe)
        audio = await ctx.read_artifact(request.audio)
        work = self.workdir(ctx, f"chunk{request.chunk_index}")
        duration = await probe_duration(audio)
        seed = seed_int(ctx.seed, request.keyframe.sha256, request.audio.sha256)
        rng = np.random.default_rng(seed)
        failure_rate = self._failure_rate(request)
        forced = {ref for ref in request.labels.get("fail_items", "").split(",") if ref}

        performed: list[TrackItem] = []
        missed: list[dict[str, str]] = []
        rates = {"native": failure_rate}
        rates.update({k: max(failure_rate, float(v)) for k, v in self._approximation_rates().items()})
        duration_cap = round(duration, 3)
        global_prompt = request.engine.get("syntax") == "mock_global_prompt"
        for entry in request.engine.get("encoded", []):
            ref, control = entry["item_ref"], str(entry.get("control", "native"))
            if ref in forced or rng.random() < rates.get(control, failure_rate):
                missed.append({"item_ref": ref, "dimension": entry["dimension"], "reason": "injected_failure"})
                continue
            whole_clip = control == "text" and global_prompt  # a global prompt conditions the whole clip
            start = 0.0 if whole_clip else entry["start_s"]
            end = duration_cap if whole_clip else entry["end_s"]
            performed.append(
                TrackItem(
                    dimension=entry["dimension"],
                    label=entry["label"],
                    start_s=round(min(float(start), duration_cap), 3),
                    end_s=round(min(float(end), duration_cap), 3),
                    intensity=float(np.clip(entry.get("intensity", 0.5) * self._coupling(control), 0.0, 1.0)),
                    item_ref=ref,
                    source=SOURCES.get(control, "native"),
                )
            )

        # the calibratable knob: head-motion gain on top of the audio coupling (mock_motion_gain)
        energy = self._audio_energy(audio)
        if "mock_motion_gain" in request.engine:
            energy = round(energy * float(request.engine["mock_motion_gain"]), 6)
        track = BehaviorTrack(
            adapter_id=self.manifest.id,
            character_key=request.behavior.visual[0].character_key
            if request.behavior and request.behavior.visual
            else None,
            duration_s=round(duration, 3),
            fps=request.fps,
            seed=seed,
            global_emotion=request.engine.get("emotion"),
            global_intensity=request.engine.get("intensity"),
            motion_energy=energy,
            performed=sorted(performed, key=lambda p: (p.start_s, p.item_ref or "")),
            missed=sorted(missed, key=lambda m: m["item_ref"]),
            # Failure injection for tests: `fail_items` drops encoded items; `emergent_glances: off`
            # removes the spontaneous glance so that a dropped gaze item has nothing to hide behind.
            emergent=self._emergent(duration, seed, energy, glances=request.labels.get("emergent_glances") != "off"),
        )

        timed: list[tuple[float, float, str]] = []
        if request.behavior is not None:
            for visual in request.behavior.visual:
                for span in visual.sub_spans:
                    if "emotion_visual" in span.labels:
                        label, _ = parse_label(span.labels["emotion_visual"])
                        timed.append((span.start_s, min(span.end_s, duration), f"state: {label}"))
        lines = [
            f"MOCK AVATAR {self.manifest.id}",
            f"shot {request.labels.get('shot', '?')}  take {request.labels.get('take', '?')}"
            f"  chunk {request.chunk_index}",
        ]
        if request.labels.get("creator"):
            lines.append(f"creator: {request.labels['creator']}")
        video = work / "clip.mp4"
        await render_avatar_clip(
            keyframe,
            audio,
            video,
            width=request.width,
            height=request.height,
            fps=request.fps,
            lines=lines,
            timed_labels=timed,
            workdir=work,
        )
        track_path = work / "behavior_track.json"
        track_path.write_text(
            json.dumps(track.model_dump(mode="json", by_alias=True), indent=1, sort_keys=True), encoding="utf-8"
        )
        video_ref = await self.write(ctx, video, "video", role="video", mime="video/mp4", duration_s=duration)
        track_ref = await self.write(ctx, track_path, "other", role="behavior_track", mime="application/json")
        return AvatarResult(
            video=video_ref,
            duration_s=duration,
            fps=request.fps,
            width=request.width - request.width % 2,
            height=request.height - request.height % 2,
            behavior_track=track_ref,
        )


class MockAvatarGlobal(_MockAvatar):
    translator = GlobalPromptTranslator()


class MockAvatarSegment(_MockAvatar):
    translator = SegmentTranslator()
