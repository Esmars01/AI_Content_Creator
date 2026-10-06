"""The bundled font chain (§27): Noto Sans (Latin, Latin Extended, Greek, Cyrillic) and Noto Sans
Arabic in `assets/fonts` (SIL OFL 1.1), plus DejaVu from the image as the last resort.

- `fonts_dir()`: `CE_FONTS_DIR`, else the repository's `assets/fonts`, else `/app/assets/fonts`;
- `families()`: family name → TrueType file, read from the fonts' name tables;
- `pick_font(chain, text)`: the first family of a caption style's chain (`font_family` then
  `font_fallbacks`) that is available and covers every character of `text`; else the available
  family covering the most characters;
- `missing_glyphs(text, chain)`: characters no available font of the chain covers (caption QC).

libass renders with `fontsdir=fonts_dir()`, so the family named in the ASS style is the bundled file.
"""

from __future__ import annotations

import os
import unicodedata
from functools import lru_cache
from pathlib import Path

__all__ = ["coverage", "families", "fonts_dir", "missing_glyphs", "pick_font", "resolve_chain"]

_REPO_FONTS = Path(__file__).resolve().parents[5] / "assets" / "fonts"
_SYSTEM = {"DejaVu Sans": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf")}


def fonts_dir() -> Path:
    for candidate in (os.environ.get("CE_FONTS_DIR"), _REPO_FONTS, Path("/app/assets/fonts")):
        if candidate and Path(candidate).is_dir() and any(Path(candidate).glob("*.ttf")):
            return Path(candidate)
    return _REPO_FONTS


@lru_cache(maxsize=4)
def _families(directory: str) -> dict[str, list[str]]:
    from fontTools.ttLib import TTFont

    out: dict[str, list[str]] = {}
    for path in sorted(Path(directory).glob("*.tt[fc]")):
        with TTFont(str(path), lazy=True) as font:
            name = font["name"]
            family = name.getDebugName(16) or name.getDebugName(1)
        if family:
            out.setdefault(str(family), []).append(str(path))
    for family, paths in _SYSTEM.items():
        for system_path in paths:
            if Path(system_path).is_file():
                out.setdefault(family, []).append(str(system_path))
                break
    return out


def families() -> dict[str, list[str]]:
    return _families(str(fonts_dir()))


@lru_cache(maxsize=16)
def _cmap(path: str) -> frozenset[int]:
    from fontTools.ttLib import TTFont

    with TTFont(path, lazy=True) as font:
        return frozenset(font.getBestCmap() or {})


def coverage(family: str) -> frozenset[int]:
    out: set[int] = set()
    for path in families().get(family, []):
        out |= _cmap(path)
    return frozenset(out)


def resolve_chain(chain: list[str]) -> list[str]:
    """The families of `chain` that are available, in order, with DejaVu Sans last."""
    known = families()
    out = [f for f in chain if f in known]
    if "DejaVu Sans" in known and "DejaVu Sans" not in out:
        out.append("DejaVu Sans")
    return out


def _needed(text: str) -> set[int]:
    return {ord(c) for c in text if not c.isspace() and unicodedata.category(c)[0] != "C"}


def missing_glyphs(text: str, chain: list[str]) -> list[str]:
    needed = _needed(text)
    for family in resolve_chain(chain):
        needed -= coverage(family)
    return sorted(chr(c) for c in needed)


def pick_font(chain: list[str], text: str) -> str:
    available = resolve_chain(chain)
    if not available:
        return chain[0] if chain else "DejaVu Sans"
    needed = _needed(text)
    for family in available:
        if needed <= coverage(family):
            return family
    return max(available, key=lambda f: (len(needed & coverage(f)), -available.index(f)))
