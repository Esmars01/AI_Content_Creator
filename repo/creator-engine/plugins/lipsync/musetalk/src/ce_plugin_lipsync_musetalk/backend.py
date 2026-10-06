"""The real MuseTalk 1.5 backend: imported only on a `lipsync` worker whose image has the MuseTalk
repository at the pinned commit on `PYTHONPATH` (`musetalk.models`, `musetalk.utils`) and
`mediapipe`. Model loading and inference follow `scripts/inference.py` (v15, fp16 optional);
face boxes come from MediaPipe Face Landmarker (see `patch.py`, DECISIONS D93).

Status: implemented against the upstream code, **untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ce_plugin_lipsync_musetalk.patch import Face

__all__ = ["FACE_OVAL", "NOSE_BRIDGE", "MuseTalkBackend", "musetalk_box"]

# MediaPipe face-mesh silhouette, in order around the face.
FACE_OVAL = (
    10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377,
    152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109,
)  # fmt: skip
NOSE_BRIDGE = 195  # closest mesh point to the 68-point landmark #29 MuseTalk mirrors about [RV]


def musetalk_box(points: np.ndarray) -> tuple[int, int, int, int]:
    """`get_landmark_and_bbox` on mesh points (pixels): x from the outline's extent, the bottom at
    the chin, the top mirrored about the nose bridge."""
    outline = points[list(FACE_OVAL)]
    x1, x2 = float(outline[:, 0].min()), float(outline[:, 0].max())
    bottom = float(outline[:, 1].max())
    nose_y = float(points[NOSE_BRIDGE, 1])
    top = max(0.0, nose_y - (bottom - nose_y))
    return (int(x1), int(top), int(x2), int(bottom))


class MuseTalkBackend:
    def __init__(
        self, *, unet_dir: Path, vae_dir: Path, whisper_dir: Path, landmarker: Path, defaults: dict[str, Any]
    ) -> None:
        import torch
        from musetalk.models.unet import PositionalEncoding, UNet
        from musetalk.models.vae import VAE
        from musetalk.utils.audio_processor import AudioProcessor
        from transformers import WhisperModel

        self.torch = torch
        self.device = torch.device("cuda:0")
        self.landmarker_path = str(landmarker)
        self.vae = VAE(model_path=str(vae_dir), use_float16=False)
        self.unet = UNet(
            unet_config=str(Path(unet_dir) / "musetalkV15" / "musetalk.json"),
            model_path=str(Path(unet_dir) / "musetalkV15" / "unet.pth"),
            device=self.device,
        )
        self.pe = PositionalEncoding(d_model=384)
        if defaults.get("use_float16", True):
            self.pe = self.pe.half()
            self.vae.vae = self.vae.vae.half()
            self.unet.model = self.unet.model.half()
        self.pe = self.pe.to(self.device)
        self.vae.vae = self.vae.vae.to(self.device)
        self.unet.model = self.unet.model.to(self.device)
        self.dtype = self.unet.model.dtype
        self.audio = AudioProcessor(feature_extractor_path=str(whisper_dir))
        self.whisper = WhisperModel.from_pretrained(str(whisper_dir)).to(device=self.device, dtype=self.dtype).eval()
        self.whisper.requires_grad_(False)
        self.timesteps = torch.tensor([0], device=self.device)
        self.padding = (int(defaults.get("audio_padding_left", 2)), int(defaults.get("audio_padding_right", 2)))

    def detect_faces(self, frame_paths: list[str]) -> list[list[Face]]:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions
        from PIL import Image

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=self.landmarker_path),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=4,
        )
        out: list[list[Face]] = []
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            for i, path in enumerate(frame_paths):
                frame = np.asarray(Image.open(path).convert("RGB"))
                height, width = frame.shape[:2]
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame)), i * 40
                )
                faces = []
                for landmarks in result.face_landmarks:
                    points = np.asarray([(p.x * width, p.y * height) for p in landmarks], dtype=np.float64)
                    outline = tuple((float(x), float(y)) for x, y in points[list(FACE_OVAL)])
                    faces.append(Face(musetalk_box(points), outline))
                out.append(faces)
        return out

    def audio_features(self, wav16k: str, fps: float, frames: int) -> Any:
        features, length = self.audio.get_audio_feature(wav16k)
        chunks = self.audio.get_whisper_chunk(
            features, self.device, self.dtype, self.whisper, length, fps=int(fps),
            audio_padding_length_left=self.padding[0], audio_padding_length_right=self.padding[1],
        )  # fmt: skip
        if chunks.shape[0] < frames:  # floor rounding at the tail: repeat the last window
            chunks = self.torch.cat([chunks, chunks[-1:].repeat(frames - chunks.shape[0], 1, 1)], dim=0)
        return chunks

    def generate(self, crops: list[np.ndarray], features: Any, indices: list[int], seed: int) -> list[np.ndarray]:
        torch = self.torch
        torch.manual_seed(seed)  # the VAE encoder samples its latent distribution
        with torch.no_grad():
            latents = torch.cat(
                [self.vae.get_latents_for_unet(np.ascontiguousarray(c[:, :, ::-1])) for c in crops], dim=0
            ).to(dtype=self.dtype)
            audio = self.pe(features[indices].to(self.device))
            pred = self.unet.model(latents, self.timesteps, encoder_hidden_states=audio).sample
            decoded = self.vae.decode_latents(pred)  # BGR uint8
        return [np.ascontiguousarray(frame[:, :, ::-1]) for frame in decoded]

    def close(self) -> None:
        self.unet = self.vae = self.whisper = None  # type: ignore[assignment]
        self.torch.cuda.empty_cache()
