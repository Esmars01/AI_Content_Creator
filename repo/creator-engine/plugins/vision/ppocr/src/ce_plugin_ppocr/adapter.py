"""OCR of sampled frames (video) or one image: boxes as normalized (x, y, w, h) with text and score."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import VisionAnalyzer
from ce_contracts.models import OcrBox, OcrFrame, OcrRequest, OcrResult
from PIL import Image

__all__ = ["PPOcr", "frames_at"]


def frames_at(path: Path, fps: float, max_side: int) -> list[tuple[float, np.ndarray]]:
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return [(0.0, np.asarray(Image.open(path).convert("RGB")))]
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise RuntimeError("ffmpeg is not installed")
    raw = subprocess.run(  # noqa: S603 - fixed argv over a downloaded artifact
        [
            binary,
            "-hide_banner",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"fps={fps:g},scale='min({max_side},iw)':-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        capture_output=True,
        check=True,
        timeout=600,
    ).stdout
    marker = b"\x89PNG\r\n\x1a\n"
    return [
        (round(i / fps, 3), np.asarray(Image.open(BytesIO(marker + chunk)).convert("RGB")))
        for i, chunk in enumerate(raw.split(marker)[1:])
    ]


class PPOcr(VisionAnalyzer):
    seconds_per_unit = 0.8

    def __init__(self, manifest: Any) -> None:
        super().__init__(manifest)
        self.config: dict[str, Any] = dict(manifest.defaults)
        self._engine: Any = None
        self._lock = asyncio.Lock()

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.config.update(ctx.config)

        root = Path(ctx.model_cache_dir)
        det_dir, rec_dir = root / str(self.config["det_dir"]), root / str(self.config["rec_dir"])
        keys = Path(ctx.scratch_dir) / "ppocr" / "rec_keys.txt"

        def open_engine() -> Any:
            import yaml
            from rapidocr_onnxruntime import RapidOCR

            rec = yaml.safe_load((rec_dir / "inference.yml").read_text(encoding="utf-8"))
            det = yaml.safe_load((det_dir / "inference.yml").read_text(encoding="utf-8"))
            keys.parent.mkdir(parents=True, exist_ok=True)
            keys.write_text("\n".join(rec["PostProcess"]["character_dict"]) + "\n", encoding="utf-8")
            post = det.get("PostProcess", {})
            return RapidOCR(
                det_model_path=str(det_dir / "inference.onnx"),
                rec_model_path=str(rec_dir / "inference.onnx"),
                rec_keys_path=str(keys),
                det_thresh=float(post.get("thresh", 0.3)),
                det_box_thresh=float(post.get("box_thresh", 0.5)),
                det_unclip_ratio=float(post.get("unclip_ratio", 1.6)),
            )

        self._engine = await asyncio.to_thread(open_engine)

    def recognize(self, rgb: np.ndarray) -> list[OcrBox]:
        h, w = rgb.shape[:2]
        result, _ = self._engine(np.ascontiguousarray(rgb[:, :, ::-1]))  # RapidOCR reads BGR arrays
        boxes: list[OcrBox] = []
        for quad, text, score in result or []:
            if float(score) < float(self.config["min_confidence"]) or not str(text).strip():
                continue
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
            x0, y0 = max(0.0, min(xs)), max(0.0, min(ys))
            boxes.append(
                OcrBox(
                    text=str(text),
                    bbox=(
                        round(x0 / w, 4),
                        round(y0 / h, 4),
                        round((max(xs) - x0) / w, 4),
                        round((max(ys) - y0) / h, 4),
                    ),
                    confidence=round(min(1.0, float(score)), 4),
                )
            )
        return boxes

    def _run(self, path: Path, fps: float) -> list[OcrFrame]:
        return [
            OcrFrame(t_s=t, boxes=self.recognize(rgb)) for t, rgb in frames_at(path, fps, int(self.config["max_side"]))
        ]

    async def run_vision_ocr(self, request: OcrRequest, ctx: RunContext) -> OcrResult:
        if self._engine is None:
            raise RuntimeError("ppocr is not loaded")
        path = await ctx.read_artifact(request.media)
        async with self._lock:
            frames = await asyncio.to_thread(self._run, path, request.sampling_fps)
        return OcrResult(frames=frames)
