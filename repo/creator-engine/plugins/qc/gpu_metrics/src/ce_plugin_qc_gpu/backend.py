"""Real backends of the model-based QC metrics, imported only on GPU workers whose image carries
the upstream code at the pinned commits. **Untested on a GPU** (rule 5).

- SyncNet: `SyncNetInstance` / `S` from joonson/syncnet_python@907c0b5; the forward passes mirror
  `SyncNetInstance.evaluate` (5-frame lip windows, 20-step MFCC windows, `calc_pdist`, offset at the
  minimum mean distance, confidence = median − minimum) on crops the adapter cut; face boxes from
  MediaPipe Face Landmarker instead of S3FD (D93).
- UVQ 1.5: `uvq1p5_pytorch.utils.uvq1p5.UVQ1p5().infer(...)` from google/uvq@c204ecc (the
  checkpoints are part of that repository).
- UTMOSv2: `utmosv2.create_model(checkpoint_path=...)` from sarulab-speech/UTMOSv2@cc2700d with the
  SSL encoder read from the model cache and the image backbone built without its ImageNet download
  (the fusion checkpoint overwrites those weights)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["SyncNetBackend", "UTMOSBackend", "UVQBackend"]


def _on_path(code_dir: str) -> None:
    if code_dir not in sys.path:
        sys.path.insert(0, code_dir)


class SyncNetBackend:
    def __init__(self, *, weights: Path, landmarker: Path, code_dir: str) -> None:
        _on_path(code_dir)
        import torch
        from SyncNetInstance import SyncNetInstance

        self.torch = torch
        self.net = SyncNetInstance()
        self.net.loadParameters(str(weights))
        self.net.eval()
        self.landmarker = str(landmarker)

    def face_boxes(self, frame_paths: list[str]) -> list[tuple[float, float, float, float] | None]:
        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions
        from PIL import Image

        options = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=self.landmarker),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
        )
        out: list[tuple[float, float, float, float] | None] = []
        with vision.FaceLandmarker.create_from_options(options) as landmarker:
            for i, path in enumerate(frame_paths):
                frame = np.asarray(Image.open(path).convert("RGB"))
                h, w = frame.shape[:2]
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(frame)), i * 40
                )
                if not result.face_landmarks:
                    out.append(None)
                    continue
                pts = np.asarray([(p.x * w, p.y * h) for p in result.face_landmarks[0]])
                out.append(
                    (float(pts[:, 0].min()), float(pts[:, 1].min()), float(pts[:, 0].max()), float(pts[:, 1].max()))
                )
        return out

    def evaluate(self, crops_rgb: np.ndarray, audio16k: np.ndarray, vshift: int) -> tuple[int, float, float]:
        import python_speech_features
        from SyncNetInstance import calc_pdist

        torch = self.torch
        model = self.net.__S__
        device = self.net.device
        im = np.ascontiguousarray(crops_rgb[..., ::-1])  # the model was trained on BGR frames (cv2)
        imtv = torch.from_numpy(np.transpose(im[None], (0, 4, 1, 2, 3)).astype(np.float32))  # 1 3 T H W
        pcm = np.clip(audio16k * 32768.0, -32768, 32767).astype(np.int16)
        mfcc = np.stack([np.array(c) for c in zip(*python_speech_features.mfcc(pcm, 16000), strict=False)])
        cct = torch.from_numpy(mfcc[None, None].astype(np.float32))
        length = min(len(crops_rgb), len(pcm) // 640)
        last = length - 5
        im_feat, cc_feat = [], []
        with torch.no_grad():
            for i in range(0, last, 20):
                stop = min(last, i + 20)
                im_in = torch.cat([imtv[:, :, v : v + 5] for v in range(i, stop)], 0)
                cc_in = torch.cat([cct[:, :, :, v * 4 : v * 4 + 20] for v in range(i, stop)], 0)
                im_feat.append(model.forward_lip(im_in.to(device)).cpu())
                cc_feat.append(model.forward_aud(cc_in.to(device)).cpu())
        dists = calc_pdist(torch.cat(im_feat, 0), torch.cat(cc_feat, 0), vshift=vshift)
        mdist = torch.mean(torch.stack(dists, 1), 1)
        minval, minidx = torch.min(mdist, 0)
        return int(vshift - int(minidx)), float(torch.median(mdist) - minval), float(minval)

    def close(self) -> None:
        self.net = None


class UVQBackend:
    def __init__(self, *, code_dir: str) -> None:
        _on_path(code_dir)
        import torch
        from uvq1p5_pytorch.utils import uvq1p5

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = uvq1p5.UVQ1p5()
        if self.device == "cuda":
            self.model.cuda()

    def score(self, video: str, length_s: int, transpose: bool, fps: int) -> dict[str, Any]:
        result: dict[str, Any] = self.model.infer(video, length_s, transpose, fps=fps, device=self.device)
        return result

    def close(self) -> None:
        self.model = None


class UTMOSBackend:
    def __init__(self, *, checkpoint: Path, ssl_dir: Path, repetitions: int) -> None:
        import timm
        import torch
        import utmosv2
        import utmosv2.config.fusion_stage3 as stage3
        import utmosv2.model.multi_spec as multi_spec

        self.torch = torch
        stage3.model.ssl.name = str(ssl_dir)  # the SSL encoder from the model cache, not the Hub
        create = timm.create_model

        def offline(*args: Any, **kwargs: Any) -> Any:  # the fusion checkpoint replaces these weights
            kwargs["pretrained"] = False
            return create(*args, **kwargs)

        multi_spec.timm.create_model = offline  # type: ignore[attr-defined]
        try:
            self.model = utmosv2.create_model(pretrained=True, checkpoint_path=str(checkpoint))
        finally:
            multi_spec.timm.create_model = create  # type: ignore[attr-defined]
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.repetitions = repetitions

    def predict(self, samples16k: np.ndarray) -> float:
        data = self.torch.from_numpy(np.ascontiguousarray(samples16k, dtype=np.float32))
        mos = self.model.predict(
            data=data, sr=16000, device=self.device, num_repetitions=self.repetitions, verbose=False
        )
        return float(np.asarray(mos.cpu() if hasattr(mos, "cpu") else mos).reshape(-1)[0])

    def close(self) -> None:
        self.model = None
