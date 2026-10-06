#!/usr/bin/env python3
"""Generates the synthetic, license-free config assets: room impulse responses and LUTs.

Deterministic (seeded per asset id), so re-running produces identical files; `--check`
verifies the committed files match. Real recorded IRs or graded LUTs can replace them later
(their license must be recorded, rule 15).

- config/rooms/impulse_responses/<room>.wav: 48 kHz mono 16-bit, exponentially decaying
  noise with the room's RT60 (60 dB decay) and a direct-sound impulse.
- config/luts/<name>.cube: 17-point 3D LUTs (Adobe .cube) with gentle, documented transforms.
"""

from __future__ import annotations

import hashlib
import io
import math
import random
import struct
import sys
import wave
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "py" / "ce_core" / "src"))
from ce_core.yamlio import load_yaml_file  # noqa: E402

SAMPLE_RATE = 48_000
LUT_SIZE = 17


def impulse_response(room_id: str, rt60_s: float) -> bytes:
    seed = int(hashlib.sha256(room_id.encode()).hexdigest()[:16], 16)
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic noise, not cryptography
    length = int(SAMPLE_RATE * min(max(rt60_s * 1.5, 0.05), 2.0))
    decay = math.log(1000.0) / (rt60_s * SAMPLE_RATE)  # amplitude falls 60 dB over rt60
    samples = [1.0]
    samples += [rng.uniform(-1.0, 1.0) * 0.5 * math.exp(-decay * n) for n in range(1, length)]
    peak = max(abs(s) for s in samples)
    frames = b"".join(struct.pack("<h", round(s / peak * 32000)) for s in samples)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)
    return buffer.getvalue()


def _clamp(x: float) -> float:
    return min(1.0, max(0.0, x))


def _saturate(r: float, g: float, b: float, amount: float) -> tuple[float, float, float]:
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return tuple(_clamp(luma + (c - luma) * amount) for c in (r, g, b))  # type: ignore[return-value]


def _contrast(c: float, amount: float) -> float:
    return _clamp(0.5 + (c - 0.5) * amount)


Transform = Callable[[float, float, float], tuple[float, float, float]]

LUTS: dict[str, tuple[str, Transform]] = {
    "phone_natural": (
        "slight warmth and saturation, like a phone's default processing",
        lambda r, g, b: _saturate(_clamp(r * 1.02 + 0.005), g, _clamp(b * 0.98), 1.06),
    ),
    "webcam_flat": (
        "lower contrast with a faint green-cyan cast",
        lambda r, g, b: (_contrast(r * 0.98, 0.9), _contrast(_clamp(g * 1.01), 0.9), _contrast(b, 0.9)),
    ),
    "mirrorless_neutral": (
        "neutral with a gentle contrast lift",
        lambda r, g, b: (_contrast(r, 1.04), _contrast(g, 1.04), _contrast(b, 1.04)),
    ),
    "cinematic_warm": (
        "mild teal-orange split: warm highlights, cooler shadows",
        lambda r, g, b: _saturate(_clamp(r + 0.03 * r), _clamp(g + 0.005), _clamp(b + 0.03 * (1 - b) - 0.02 * b), 0.97),
    ),
    "screen_neutral": ("identity (screen recordings are not graded)", lambda r, g, b: (r, g, b)),
}


def lut_text(name: str, description: str, transform: Transform) -> str:
    lines = [
        f'TITLE "{name}"',
        f"# {description} (synthetic, scripts/gen_synthetic_assets.py)",
        f"LUT_3D_SIZE {LUT_SIZE}",
    ]
    lines += ["DOMAIN_MIN 0.0 0.0 0.0", "DOMAIN_MAX 1.0 1.0 1.0"]
    step = 1.0 / (LUT_SIZE - 1)
    for bi in range(LUT_SIZE):
        for gi in range(LUT_SIZE):
            for ri in range(LUT_SIZE):  # red varies fastest (.cube convention)
                r, g, b = transform(ri * step, gi * step, bi * step)
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return "\n".join(lines) + "\n"


def outputs() -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for room_file in sorted((ROOT / "config" / "rooms").glob("*.yaml")):
        room = load_yaml_file(room_file)
        files[ROOT / "config" / "rooms" / room["impulse_response"]] = impulse_response(
            room["id"], float(room["rt60_s"])
        )
    for name, (description, transform) in LUTS.items():
        files[ROOT / "config" / "luts" / f"{name}.cube"] = lut_text(name, description, transform).encode()
    return files


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for path, content in outputs().items():
        if check:
            if not path.exists() or path.read_bytes() != content:
                stale.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    if stale:
        print(f"stale synthetic assets (run scripts/gen_synthetic_assets.py): {stale}")
        return 1
    print("synthetic assets " + ("up to date" if check else "written"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
