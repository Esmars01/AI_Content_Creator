"""AuraFace embeddings.

Images and video frames (`face.embed` takes one medium): the MediaPipe Face Landmarker finds the
face; eye centres (iris 468/473), nose tip (1) and mouth corners (61/291) are mapped onto the ArcFace
112×112 template by a similarity transform; glintr100 embeds the aligned crop ((x − 127.5) / 127.5,
RGB). Video returns the L2-normalized mean over up to `max_frames` frames. No face → an empty
`vectors` list (the caller reports NOT_MEASURABLE, never a match).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import FaceAnalyzer
from ce_contracts.models import EmbeddingResult, MediaAnalysisRequest
from PIL import Image

__all__ = ["ARCFACE_TEMPLATE", "AuraFace", "align"]

ARCFACE_TEMPLATE = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)
_POINTS = (468, 473, 1, 61, 291)  # image-left eye, image-right eye, nose tip, mouth corners


def align(rgb: np.ndarray, points: np.ndarray) -> np.ndarray:
    import cv2

    matrix, _ = cv2.estimateAffinePartial2D(points.astype(np.float32), ARCFACE_TEMPLATE, method=cv2.LMEDS)
    if matrix is None:
        raise ValueError("face alignment failed")
    return cv2.warpAffine(rgb, matrix, (112, 112), borderValue=0.0)


def _frames(path: Path, hz: float, limit: int) -> list[np.ndarray]:
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return [np.asarray(Image.open(path).convert("RGB"))]
    import shutil
    import subprocess

    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    out: list[np.ndarray] = []
    probe = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"fps={hz:g}",
            "-frames:v",
            str(limit),
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=300,
    ).stdout
    from io import BytesIO

    marker = b"\x89PNG\r\n\x1a\n"
    for chunk in probe.split(marker)[1:]:
        out.append(np.asarray(Image.open(BytesIO(marker + chunk)).convert("RGB")))
    return out


class AuraFace(FaceAnalyzer):
    seconds_per_unit = 0.3

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self.root = Path(".")
        self._session: Any = None
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)
        self.root = Path(ctx.model_cache_dir)
        model = self.root / str(self.config["model_file"])
        for path in (model, self.root / str(self.config["landmarker_file"])):
            if not path.is_file():
                raise FileNotFoundError(f"{path} is missing: run `make fetch-cpu-assets`")

        def open_session() -> Any:
            import onnxruntime as ort

            options = ort.SessionOptions()
            options.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
            return ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])

        self._session = await asyncio.to_thread(open_session)

    def _embed(self, path: Path) -> list[list[float]]:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        frames = _frames(path, float(self.config["sample_hz"]), int(self.config["max_frames"]))
        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(self.root / str(self.config["landmarker_file"]))),
            running_mode=vision.RunningMode.IMAGE,
            num_faces=1,
        )
        vectors: list[np.ndarray] = []
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            for rgb in frames:
                result = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
                if not result.face_landmarks:
                    continue
                lm = result.face_landmarks[0]
                h, w = rgb.shape[:2]
                points = np.array([[lm[i].x * w, lm[i].y * h] for i in _POINTS], dtype=np.float32)
                crop = align(rgb, points).astype(np.float32)
                blob = ((crop - 127.5) / 127.5).transpose(2, 0, 1)[None]
                (vec,) = self._session.run(None, {self._session.get_inputs()[0].name: blob})
                v = np.asarray(vec)[0]
                vectors.append(v / (np.linalg.norm(v) or 1.0))
        if not vectors:
            return []
        mean = np.mean(vectors, axis=0)
        mean = mean / (np.linalg.norm(mean) or 1.0)
        return [[round(float(x), 6) for x in mean]]

    async def run_face_embed(self, request: MediaAnalysisRequest, ctx: RunContext) -> EmbeddingResult:
        if self._session is None:
            raise RuntimeError("auraface is not loaded")
        path = await ctx.read_artifact(request.media)
        async with self._lock:
            vectors = await asyncio.to_thread(self._embed, path)
        return EmbeddingResult(vectors=vectors, dim=512)
