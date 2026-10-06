"""Director stage 12 — packaging limits and export presets (Phase 12): verified platform rules or
our labelled design defaults; every limit enforced on titles, descriptions, hashtags, CTAs and
thumbnail texts; the template packaging always fits; every platform's export presets are coherent
with its rules; thumbnail frame times stay inside the video."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from ce_config.schemas import PackagingDesignLimits
from ce_director.packaging import (
    PlatformPackagingOut,
    effective_limits,
    normalize_hashtags,
    problems,
    spec_summary,
    template_packaging,
)
from ce_exec.packaging_job import thumbnail_times
from ce_testing.build import config_bundle
from ce_testing.fixtures import example_spec_dict

pytestmark = [pytest.mark.behavior]

BUNDLE = config_bundle()
DESIGN = PackagingDesignLimits(
    title_max_chars=50, description_max_chars=120, hashtags_max=3, hashtag_max_chars=12, cta_max_chars=20
)


def _platform(verified: bool, **rules: int | None) -> SimpleNamespace:
    base = {"title_max_chars": None, "description_max_chars": None, "hashtags_max": None}
    return SimpleNamespace(verified_at=date(2026, 10, 1) if verified else None, rules=SimpleNamespace(**(base | rules)))


def test_unverified_platform_rules_never_count_as_platform_facts() -> None:
    unverified = effective_limits(_platform(False, title_max_chars=10), DESIGN, thumbnail_text_max_chars=30)
    assert unverified.title_max_chars == 50 and set(unverified.sources.values()) == {"design_default"}
    verified = effective_limits(_platform(True, title_max_chars=10), DESIGN, thumbnail_text_max_chars=30)
    assert verified.title_max_chars == 10 and verified.sources["title_max_chars"] == "platform"
    assert verified.sources["description_max_chars"] == "design_default"  # a null rule falls back
    for platform in BUNDLE.platforms.values():  # the shipped platform files are all unverified (D16)
        limits = effective_limits(platform, BUNDLE.app.packaging.design_limits, thumbnail_text_max_chars=40)
        assert set(limits.sources.values()) == {"design_default"}, platform.id


def test_every_limit_is_enforced() -> None:
    limits = effective_limits(_platform(False), DESIGN, thumbnail_text_max_chars=15)
    ok = PlatformPackagingOut(
        title="Agents explained", description="Short.", hashtags=["#ai", "agents"], thumbnail_texts=["Agents!"]
    )
    assert problems(ok, limits, thumbnails=1) == []
    bad = PlatformPackagingOut(
        title="x" * 51,
        description="y" * 121,
        hashtags=["a", "b", "c", "d", "two words", "averyveryverylongtag"],
        cta_text="z" * 21,
        thumbnail_texts=["much too long for a thumbnail", ""],
    )
    found = " | ".join(problems(bad, limits, thumbnails=2))
    for expected in (
        "the title has 51 characters; at most 50",
        "the description has 121 characters; at most 120",
        "6 hashtags; at most 3",
        "hashtag 'two words' must be one word",
        "hashtag 'averyveryverylongtag' is longer than 12",
        "the call to action has 21 characters; at most 20",
        "thumbnail text 'much too long for a thumbnail'",
    ):
        assert expected in found, expected
    assert problems(PlatformPackagingOut(title="Fine\nTwo lines", thumbnail_texts=["a"]), limits, thumbnails=1)
    assert problems(PlatformPackagingOut(title="Fine"), limits, thumbnails=2) == ["give 2 thumbnail texts"]


def test_hashtags_are_normalized() -> None:
    assert normalize_hashtags(["#AI", "ai", " ＃Agents ", "", "#"]) == ["AI", "Agents"]


def test_the_template_packaging_always_fits_its_limits() -> None:
    summary = spec_summary(example_spec_dict())
    for design in (DESIGN, PackagingDesignLimits(title_max_chars=12, description_max_chars=30, hashtags_max=1)):
        limits = effective_limits(_platform(False), design, thumbnail_text_max_chars=16)
        draft = template_packaging(summary, "tiktok", limits, thumbnails=3)
        out = PlatformPackagingOut(
            title=draft.title,
            description=draft.description,
            hashtags=draft.hashtags,
            cta_text=draft.cta_text,
            thumbnail_texts=draft.thumbnail_texts,
        )
        assert problems(out, limits, thumbnails=3) == [], (design, draft)
        assert draft.generator["kind"] == "template" and draft.title
    assert summary["hook"] and summary["script"].startswith(example_spec_dict()["script"]["segments"][0]["text"])


def test_export_presets_match_their_platforms() -> None:
    """Every export preset is unique, has an even size matching its aspect and an aspect the
    platform's rules allow; packaging thumbnails have a size."""
    seen: set[str] = set()
    for platform in BUNDLE.platforms.values():
        assert platform.render_presets, platform.id
        assert platform.export_checklist and all(
            i.required for i in platform.export_checklist if i.key == "not_mass_produced"
        )
        for preset in platform.render_presets:
            assert preset.id not in seen, preset.id
            seen.add(preset.id)
            assert preset.width % 2 == 0 and preset.height % 2 == 0
            w, h = (int(x) for x in preset.aspect.split(":"))
            assert abs(preset.width / preset.height - w / h) < 0.01, preset.id
            assert preset.aspect in platform.rules.aspects, (platform.id, preset.aspect)
            assert BUNDLE.render_preset(preset.id) is preset
        assert platform.packaging.thumbnail_width > 0 and platform.packaging.thumbnail_height > 0


def test_thumbnail_times_stay_inside_the_video() -> None:
    shots = {"sht_1": [0.0, 4.0], "sht_2": [4.0, 5.0], "sht_3": [5.0, 12.0]}
    times = thumbnail_times(shots, 12.0, 3)
    assert times[0] == 0.5 and times == sorted(times) and len(times) == 3
    assert 8.5 in times and 2.0 in times  # midpoints of the two longest shots
    short = thumbnail_times({}, 2.0, 3)
    assert len(short) == 3 and all(0 <= t < 2.0 for t in short)
    assert thumbnail_times({}, 0.0, 2) == [0.0]
