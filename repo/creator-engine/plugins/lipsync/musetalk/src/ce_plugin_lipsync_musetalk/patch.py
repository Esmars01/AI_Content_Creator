"""The CPU half of the MuseTalk patch: face boxes, temporal smoothing, frame cycling, crops and
the feathered lower-face blend. Mirrors `scripts/inference.py` and `musetalk/utils/blending.py` at
commit 0a89dec, with two deliberate differences (DECISIONS D93):

- boxes and the face outline come from MediaPipe Face Landmarker instead of DWPose + S3FD (the
  same construction: landmark x-extent, chin as the bottom, the upper edge mirrored about the
  nose bridge), so the image needs no mmcv/mmpose and no unversioned detector download;
- the blend mask is the face outline below the box's middle, feathered (MuseTalk's `jaw` mode
  uses a BiSeNet face parser whose weights are distributed from a Google Drive link with no
  stated license; `blend_mask: parsing` stays unavailable until the owner verifies them).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

__all__ = ["Face", "blend", "crop_box", "expanded_box", "lower_face_mask", "pingpong", "select_face", "smooth_boxes"]

Box = tuple[int, int, int, int]


@dataclass(frozen=True)
class Face:
    box: Box  # x1, y1, x2, y2 in pixels (MuseTalk's landmark box)
    outline: tuple[tuple[float, float], ...] = ()  # the face oval in pixels, in order


def pingpong(index: int, count: int) -> int:
    """Upstream cycles `frames + frames[::-1]` when the audio outlasts the video."""
    if count <= 0:
        raise ValueError("no frames")
    k = index % (2 * count)
    return k if k < count else 2 * count - 1 - k


def select_face(
    faces: Sequence[Face], region: tuple[float, float, float, float] | None, size: tuple[int, int]
) -> Face | None:
    """The face to patch: the largest one, or — with a normalized `(x0, y0, x1, y1)` region — the
    largest whose center lies inside it."""
    width, height = size
    candidates = list(faces)
    if region is not None:
        x0, y0, x1, y1 = region[0] * width, region[1] * height, region[2] * width, region[3] * height
        candidates = [
            f for f in candidates if x0 <= (f.box[0] + f.box[2]) / 2 <= x1 and y0 <= (f.box[1] + f.box[3]) / 2 <= y1
        ]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.box[2] - f.box[0]) * (f.box[3] - f.box[1]))


def smooth_boxes(boxes: Sequence[Box | None], window: int = 5) -> list[Box | None]:
    """A centered moving average over the frames that have a face (frames without one stay None):
    landmark boxes jitter by a few pixels per frame, which shows as a shimmering mouth edge."""
    half = max(0, window // 2)
    out: list[Box | None] = []
    for i, box in enumerate(boxes):
        if box is None:
            out.append(None)
            continue
        near = [b for b in boxes[max(0, i - half) : i + half + 1] if b is not None]
        mean = np.mean(np.asarray(near, dtype=np.float64), axis=0)
        out.append((round(mean[0]), round(mean[1]), round(mean[2]), round(mean[3])))
    return out


def crop_box(box: Box, size: tuple[int, int], extra_margin: int) -> Box:
    """The region the UNet repaints: the landmark box with `extra_margin` px more chin (v1.5),
    clipped to the frame."""
    width, height = size
    x1, y1, x2, y2 = box
    return (max(0, x1), max(0, y1), min(width, x2), min(height, y2 + extra_margin))


def expanded_box(box: Box, expand: float = 1.5) -> Box:
    """`get_crop_box`: a square of side `max(w, h) * expand` centered on the box."""
    x1, y1, x2, y2 = box
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    s = int(max(x2 - x1, y2 - y1) // 2 * expand)
    return (cx - s, cy - s, cx + s, cy + s)


def lower_face_mask(face: Face, box: Box, region: Box, upper_ratio: float = 0.5) -> Image.Image:
    """The blend mask over `region` (the expanded square): the face outline (or the ellipse
    inscribed in the box without one) inside the repaint box, rows above `upper_ratio` of the
    region dropped, feathered like upstream (Gaussian, kernel 5% of the region)."""
    rx1, ry1, rx2, ry2 = region
    size = (rx2 - rx1, ry2 - ry1)
    shape = Image.new("L", size, 0)
    draw = ImageDraw.Draw(shape)
    if len(face.outline) >= 3:
        draw.polygon([(x - rx1, y - ry1) for x, y in face.outline], fill=255)
    else:
        draw.ellipse([box[0] - rx1, box[1] - ry1, box[2] - rx1, box[3] - ry1], fill=255)
    inside = Image.new("L", size, 0)
    inside.paste(255, (box[0] - rx1, box[1] - ry1, box[2] - rx1, box[3] - ry1))
    mask = np.minimum(np.asarray(shape), np.asarray(inside))
    mask = mask.copy()
    mask[: int(size[1] * upper_ratio)] = 0
    kernel = int(0.05 * size[0] // 2 * 2) + 1
    return Image.fromarray(mask).filter(ImageFilter.GaussianBlur(radius=max(1.0, kernel / 6.0)))


def blend(frame: np.ndarray, generated: np.ndarray, face: Face, box: Box, upper_ratio: float = 0.5) -> np.ndarray:
    """Pastes the generated crop (already resized to `box`) into the frame through the lower-face
    mask (`get_image` with the expanded square, as upstream). Pixels outside the mask are the
    source frame's, bit for bit."""
    height, width = frame.shape[:2]
    region = expanded_box(box)
    body = Image.fromarray(frame)
    large = body.crop(region)  # PIL pads outside the frame with black; the mask is zero there
    large.paste(Image.fromarray(generated), (box[0] - region[0], box[1] - region[1]))
    mask = lower_face_mask(face, box, region, upper_ratio)
    body.paste(large, region[:2], mask)
    out = np.asarray(body)
    assert out.shape == (height, width, 3)
    return out
