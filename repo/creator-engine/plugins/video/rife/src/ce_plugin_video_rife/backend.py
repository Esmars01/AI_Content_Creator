"""The real Practical-RIFE 4.25 backend: the `train_log` model package (`RIFE_HDv3.Model`, its
`IFNet_HDv3` and `flownet.pkl`, distributed together as the 4.25 archive) loaded as in
`inference_video.py` at commit bbfd2ea, with the same padding to multiples of 128/scale and
arbitrary-timestep inference (`model.version >= 3.9`). Imported only on a `post` worker.
**Untested on a GPU** (rule 5)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["RifeBackend"]


class RifeBackend:
    def __init__(self, *, train_log: Path, defaults: dict[str, Any]) -> None:
        import torch

        parent = str(Path(train_log).parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        from train_log.RIFE_HDv3 import Model

        self.torch = torch
        torch.set_grad_enabled(False)
        self.model = Model()
        if not hasattr(self.model, "version"):
            self.model.version = 0
        self.model.load_model(str(train_log), -1)
        self.model.eval()
        self.model.device()
        if float(self.model.version) < 3.9:
            raise RuntimeError(f"RIFE model version {self.model.version} lacks arbitrary timesteps")
        self.scale = float(defaults.get("scale", 1.0))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def _tensor(self, frame: np.ndarray, padding: tuple[int, int, int, int]) -> Any:
        import torch.nn.functional as F

        x = (
            self.torch.from_numpy(np.ascontiguousarray(frame.transpose(2, 0, 1))).to(self.device).unsqueeze(0).float()
            / 255.0
        )
        return F.pad(x, padding)

    def interpolate(self, a: np.ndarray, b: np.ndarray, times: list[float]) -> list[np.ndarray]:
        h, w = a.shape[:2]
        tmp = max(128, int(128 / self.scale))
        ph, pw = ((h - 1) // tmp + 1) * tmp, ((w - 1) // tmp + 1) * tmp
        padding = (0, pw - w, 0, ph - h)
        i0, i1 = self._tensor(a, padding), self._tensor(b, padding)
        out = []
        for t in times:
            mid = self.model.inference(i0, i1, float(t), self.scale)
            out.append((mid[0] * 255).byte().cpu().numpy().transpose(1, 2, 0)[:h, :w])
        return out

    def close(self) -> None:
        self.model = None
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
