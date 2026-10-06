"""The bundled font chain (§27): Noto Sans and Noto Sans Arabic cover Latin Extended, Cyrillic and Arabic."""

from __future__ import annotations

from ce_render.fonts import families, fonts_dir, missing_glyphs, pick_font, resolve_chain

CHAIN = ["Inter", "Noto Sans", "Noto Sans Arabic", "DejaVu Sans"]


def test_bundled_fonts_and_licenses() -> None:
    assert {"Noto Sans", "Noto Sans Arabic"} <= set(families())
    assert (fonts_dir() / "OFL-NotoSans.txt").is_file() and (fonts_dir() / "OFL-NotoSansArabic.txt").is_file()


def test_chain_resolution_and_coverage() -> None:
    assert resolve_chain(CHAIN)[:2] == ["Noto Sans", "Noto Sans Arabic"]
    assert pick_font(CHAIN, "Most people think") == "Noto Sans"
    assert pick_font(CHAIN, "Привет ışık ə ğ ş İ") == "Noto Sans"
    assert pick_font(CHAIN, "سلام عليكم") == "Noto Sans Arabic"
    assert missing_glyphs("Bakı İstanbul Москва القاهرة", CHAIN) == []
    assert missing_glyphs("你好", CHAIN) == ["你", "好"]
