"""MuseTalk 1.5 adapter (`lipsync.dub`, §21 lip-sync patch).

The clip is resampled to MuseTalk's 25 fps, every frame's face is found (the backend), boxes are
smoothed, and for each output frame (the audio's length at 25 fps; frames cycle back and forth
when the audio is longer, as upstream) the backend repaints the 256×256 mouth crop from the audio
window and the adapter blends it back through a feathered lower-face mask. Frames without a face
pass through untouched and are counted. The result keeps the source size and frame rate and
carries the new audio.

`face_region` (normalized `x0, y0, x1, y1`) picks the face whose center lies inside it; without
one the largest face is patched."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import conform_clip, extract_frames, frames_dir_to_mp4, probe, resample_wav
from PIL import Image

from ce_plugin_lipsync_musetalk.patch import Face, blend, crop_box, pingpong, select_face, smooth_boxes

__all__ = ["MuseTalkAdapter"]

CROP = 256


class MuseTalkAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_lipsync_musetalk.backend import MuseTalkBackend

        assert self.paths is not None
        landmarker = self.paths.dependency("face_landmarker") / "face_landmarker.task"
        return MuseTalkBackend(
            unet_dir=self.paths.model("musetalk-v15"),
            vae_dir=self.paths.dependency("vae"),
            whisper_dir=self.paths.dependency("audio_encoder"),
            landmarker=landmarker,
            defaults=self.defaults,
        )

    async def run_lipsync_dub(self, request: m.LipSyncRequest, ctx: RunContext) -> m.VideoResult:
        backend = self.require_backend()
        d = self.defaults
        fps = float(d.get("fps", 25))
        work = self.scratch(ctx, f"dub-{request.labels.get('shot', 'clip')}")
        video = await ctx.read_artifact(request.video)
        audio = await ctx.read_artifact(request.audio)
        source = probe(video)
        frames = extract_frames(video, work / "frames", fps=fps)
        if not frames:
            raise ValueError("lipsync.dub: the video has no frames")
        speech = resample_wav(audio, work / "speech16k.wav", 16_000)
        audio_s = probe(speech).duration_s
        n_out = max(1, math.floor(audio_s * fps))
        size = (source.width, source.height)

        detected = await self.call(ctx, backend.detect_faces, [str(p) for p in frames])
        chosen: list[Face | None] = [select_face(faces, request.face_region, size) for faces in detected]
        boxes = smooth_boxes([f.box if f is not None else None for f in chosen], int(d.get("box_smoothing", 5)))
        features = await self.call(ctx, backend.audio_features, str(speech), fps, n_out)

        out_dir = work / "patched"
        out_dir.mkdir(exist_ok=True)
        margin, batch = int(d.get("extra_margin", 10)), int(d.get("batch_size", 8))
        patched = skipped = 0
        for start in range(0, n_out, batch):
            indices = list(range(start, min(n_out, start + batch)))
            sources = [pingpong(i, len(frames)) for i in indices]
            images = [np.asarray(Image.open(frames[s]).convert("RGB")) for s in sources]
            jobs: list[tuple[int, np.ndarray, tuple[int, int, int, int], Face]] = []
            for slot, src in enumerate(sources):
                face, box = chosen[src], boxes[src]
                if face is None or box is None:
                    continue
                region = crop_box(box, size, margin)
                if region[2] - region[0] < 8 or region[3] - region[1] < 8:
                    continue
                jobs.append((slot, images[slot], region, Face(region, face.outline)))
            crops = [
                np.asarray(
                    Image.fromarray(img[r[1] : r[3], r[0] : r[2]]).resize((CROP, CROP), Image.Resampling.LANCZOS)
                )
                for _, img, r, _ in jobs
            ]
            generated = (
                await self.call(
                    ctx, backend.generate, crops, features, [indices[j[0]] for j in jobs], int(ctx.seed) + start
                )
                if crops
                else []
            )
            for (slot, img, region, face), mouth in zip(jobs, generated, strict=True):
                resized = np.asarray(
                    Image.fromarray(np.asarray(mouth, dtype=np.uint8)).resize(
                        (region[2] - region[0], region[3] - region[1]), Image.Resampling.LANCZOS
                    )
                )
                images[slot] = blend(img, resized, face, region, float(d.get("upper_boundary_ratio", 0.5)))
            patched += len(jobs)
            skipped += len(indices) - len(jobs)
            for index, image in zip(indices, images, strict=True):
                Image.fromarray(image).save(out_dir / f"{index:08d}.png")

        raw = frames_dir_to_mp4(out_dir, work / "musetalk_raw.mp4", fps, audio=audio)
        result_path, out_fps = raw, fps
        if source.fps and abs(source.fps - fps) > 0.01:  # back to the clip's own rate and size
            result_path = conform_clip(
                raw, work / "musetalk.mp4", width=source.width, height=source.height, audio=audio, fps=source.fps
            )
            out_fps = source.fps
        info = probe(result_path)
        ref = await ctx.write_artifact(
            result_path,
            "video",
            {
                "adapter_id": self.manifest.id,
                "patched_frames": patched,
                "passthrough_frames": skipped,
                "face_ratio": round(patched / max(1, n_out), 4),
                "blend_mask": "landmark_outline",
            },
            role="video",
            mime="video/mp4",
        )
        return m.VideoResult(
            video=ref, duration_s=round(info.duration_s, 4), fps=out_fps, width=info.width, height=info.height
        )
