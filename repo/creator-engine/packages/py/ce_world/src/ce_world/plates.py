"""World plates (§19.2): the prompt of a plate candidate and the fingerprint statistics of a chosen
plate (colour statistics and a lighting estimate; the embedding comes from `image.embed`)."""

from __future__ import annotations

from typing import Any

import numpy as np
from ce_core.identity.world import WorldDNA

__all__ = ["plate_prompt", "plate_statistics"]


def plate_statistics(path: Any) -> dict[str, Any]:
    """Colour statistics and a lighting estimate of a plate (§19.2 fingerprints): mean RGB, luminance,
    a correlated colour temperature estimate (McCamy) and the left/right luminance ratio."""
    from PIL import Image

    with Image.open(path) as image:
        rgb = np.asarray(image.convert("RGB").resize((256, 256)), dtype=np.float64) / 255.0
    lin = np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    r, g, b = (float(lin[..., i].mean()) for i in range(3))
    x_ = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y_ = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z_ = 0.0193 * r + 0.1192 * g + 0.9505 * b
    total = x_ + y_ + z_
    cct = None
    if total > 0 and y_ > 0:
        x, y = x_ / total, y_ / total
        n = (x - 0.3320) / (0.1858 - y)
        cct = round(449 * n**3 + 3525 * n**2 + 6823.3 * n + 5520.33, 1)
    luminance = 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]
    half = luminance.shape[1] // 2
    left, right = float(luminance[:, :half].mean()), float(luminance[:, half:].mean())
    return {
        "mean_rgb": [round(float(rgb[..., i].mean()), 4) for i in range(3)],
        "luminance": round(float(luminance.mean()), 4),
        "cct_k": cct,
        "left_right_ratio": round(left / right, 4) if right > 0 else None,
        "method": "colour statistics on a 256×256 resample; McCamy CCT; reliability low until calibrated (§19.6)",
    }


def plate_prompt(dna: WorldDNA, position_key: str, time_of_day: str, weather: str) -> tuple[str, list[str]]:
    position = dna.camera_position(position_key)
    layout = dna.background_layouts.get(position_key)
    visible = [e for e in dna.elements if layout is None or e.key in layout.visible_elements]
    elements = [", ".join(p for p in (e.label or e.kind, e.description, e.material, e.color) if p) for e in visible]
    style = ", ".join(dna.style_tags)
    prompt = (
        f"empty {dna.kind.replace('_', ' ')} '{dna.name}', no people, {style}; "
        f"seen from {position_key.replace('_', ' ')} ({position.default_framing if position else ''}, "
        f"{position.lens_equiv_mm if position else 35} mm); {time_of_day.replace('_', ' ')}, {weather}; "
        f"elements: {'; '.join(elements)}"
    )
    return prompt, [e.key for e in visible]
