"""Temporal chunking for clip-level video models (SeedVR2 restores a whole clip at once; long clips
do not fit in memory): chunks of `4k + 1` frames (the causal VAE's temporal stride) that overlap
by a few frames, joined with a linear crossfade over the overlap so seams do not pop."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

__all__ = ["blend_overlap", "plan_chunks"]


def plan_chunks(frames: int, size: int, overlap: int) -> list[tuple[int, int]]:
    """`[start, end)` ranges covering `frames`, each at most `size` long (`size` is forced to
    `4k + 1`), consecutive ranges sharing `overlap` frames; the last range is shifted back so it is
    full-length when the clip allows (the model sees more context, the seam stays inside overlap)."""
    if frames <= 0:
        return []
    size = max(5, (size - 1) // 4 * 4 + 1)
    if frames <= size:
        return [(0, frames)]
    overlap = max(0, min(overlap, size - 1))
    step = size - overlap
    out: list[tuple[int, int]] = []
    start = 0
    while True:
        end = min(frames, start + size)
        if end == frames:
            out.append((max(0, frames - size), frames))
            break
        out.append((start, end))
        start += step
    return out


def blend_overlap(chunks: Sequence[tuple[tuple[int, int], Sequence[np.ndarray]]], frames: int) -> list[np.ndarray]:
    """One frame per index from overlapping chunk outputs: where two chunks cover a frame, a linear
    crossfade from the earlier to the later chunk across their shared frames."""
    out: list[np.ndarray | None] = [None] * frames
    covered_until = 0
    for (start, end), images in chunks:
        if len(images) != end - start:
            raise ValueError(f"chunk {start}-{end} returned {len(images)} frames")
        shared = max(0, covered_until - start)
        for k, image in enumerate(images):
            index = start + k
            current = out[index]
            if current is not None and k < shared:
                w = (k + 1) / (shared + 1)
                out[index] = np.clip(
                    current.astype(np.float32) * (1 - w) + image.astype(np.float32) * w, 0, 255
                ).astype(np.uint8)
            else:
                out[index] = np.asarray(image, dtype=np.uint8)
        covered_until = max(covered_until, end)
    if any(f is None for f in out):
        raise ValueError("chunks do not cover every frame")
    return out  # type: ignore[return-value]
