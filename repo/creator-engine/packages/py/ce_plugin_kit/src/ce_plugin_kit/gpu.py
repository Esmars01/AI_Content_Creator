"""Lazy GPU helpers for backends: device selection, seeding, peak-VRAM measurement and a
`nvidia-smi` summary for validation reports. torch is imported inside the functions, so this
module is importable on machines without it."""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from typing import Any

__all__ = [
    "cuda_available",
    "device",
    "gpu_summary",
    "peak_vram_gb",
    "reset_peak_vram",
    "seed_everything",
    "torch_dtype",
]


def _torch() -> Any:
    import torch

    return torch


def cuda_available() -> bool:
    try:
        return bool(_torch().cuda.is_available())
    except ImportError:
        return False


def device(preferred: str | None = None) -> str:
    """`cuda` when a GPU is visible (or `preferred`), else `cpu`."""
    if preferred:
        return preferred
    return "cuda" if cuda_available() else "cpu"


def torch_dtype(name: str) -> Any:
    torch = _torch()
    return {"bf16": torch.bfloat16, "bfloat16": torch.bfloat16, "fp16": torch.float16, "float16": torch.float16}.get(
        name, torch.float32
    )


def seed_everything(seed: int) -> None:
    """Seeds Python, NumPy and torch (CPU and CUDA); generation is reproducible per attempt seed (§12.5)."""
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import numpy as np

        np.random.seed(seed % (2**32))
    except ImportError:  # pragma: no cover
        pass
    try:
        torch = _torch()
    except ImportError:
        return
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def reset_peak_vram() -> None:
    try:
        torch = _torch()
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


def peak_vram_gb() -> float | None:
    """Peak allocated VRAM since the last reset, or None without CUDA."""
    try:
        torch = _torch()
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    return round(torch.cuda.max_memory_allocated() / 1024**3, 3)


def gpu_summary() -> dict[str, Any]:
    """GPU name, driver, CUDA runtime and VRAM from `nvidia-smi` (empty without a GPU)."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return {}
    try:
        out = subprocess.run(  # noqa: S603 - fixed binary
            [smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return {}
    gpus = []
    for line in out.splitlines():
        name, driver, memory = (x.strip() for x in line.split(","))
        gpus.append({"name": name, "driver": driver, "vram_gb": round(float(memory) / 1024, 1)})
    summary: dict[str, Any] = {"gpus": gpus}
    try:
        summary["cuda_runtime"] = _torch().version.cuda
        summary["torch"] = _torch().__version__
    except ImportError:
        pass
    return summary
