"""Blocklists and the testimonial guard (§32): rules, LLM classification through fixture replay,
resolutions (quote source, dramatization with disclosure) and fail-closed behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from ce_config import schemas
from ce_config.loader import load_config
from ce_core.spec.anchors import SceneSpan
from ce_core.spec.common import Effect
from ce_core.spec.videospec import QuoteSource, VideoSpec
from ce_llm import LLMProvider, PromptLibrary
from ce_plugin_llm_fixture.provider import FixtureLLMProvider
from ce_policy import BlocklistChecker, TestimonialGuard
from ce_policy.testimonial import rule_hits
from ce_testing.fixtures import example_spec

ROOT = Path(__file__).resolve().parents[4]
_POLICY = load_config(ROOT / "config", "test").testimonials
assert _POLICY is not None
POLICY: schemas.TestimonialPolicy = _POLICY
PROMPTS = PromptLibrary(ROOT / "prompts")


def spec_with(texts: list[str], *, mode: str = "ugc_selfie", **segment_updates: Any) -> VideoSpec:
    base = example_spec()
    template = base.script.segments[0]
    segments = [
        template.model_copy(update={"key": f"seg_{i + 1}", "text": t, **segment_updates.get(f"seg_{i + 1}", {})})
        for i, t in enumerate(texts)
    ]
    return base.model_copy(
        update={
            "meta": base.meta.model_copy(update={"mode": mode}),
            "script": base.script.model_copy(update={"segments": segments}),
        }
    )


def fixture_provider(tmp_path: Path, responses: Any) -> LLMProvider:
    doc = {"id": "tg_case", "kind": "authored", "inputs": [], "responses": {"testimonial_guard": responses}}
    (tmp_path / "tg_case.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return FixtureLLMProvider({"fixtures_dir": str(tmp_path)})


# ---------------------------------------------------------------------- blocklists


def test_blocklist_matches_whole_words_case_insensitively() -> None:
    checker = BlocklistChecker(protected_persons=["Jane Q Public"], terms=["miracle cure"], topics=["election"])
    texts = [
        ("/script/segments[seg_1]/text", "This MIRACLE  cure is great"),
        ("/script/segments[seg_2]/text", "Jane q public said so; elections matter"),
        ("/scenes[scn_1]/shots[sht_1]/visual/prompt_extra", "a reselection of items"),
    ]
    findings = checker.findings(texts)
    entries = sorted(f.detail["entry"] for f in findings)
    assert entries == ["Jane Q Public", "miracle cure"]  # "elections"/"reselection" are other words
    assert all(f.severity == "blocking" and not f.detail["overridable"] for f in findings)
    assert BlocklistChecker.from_config(load_config(ROOT / "config", "test").blocklists).hits(texts) == []


# ---------------------------------------------------------------------- testimonial rules


@pytest.mark.parametrize(
    "text",
    [
        "I've been using this serum for a month.",
        "I have tried it every morning.",
        "Since I started taking these, I sleep better.",
        "I bought two of them.",
        "My skin has never looked better.",
        "Honestly this changed my mornings.",
        "I swear by this.",
    ],
)
def test_rules_catch_first_person_experience(text: str) -> None:
    assert rule_hits(POLICY, text)


@pytest.mark.parametrize(
    "text",
    ["This serum contains 2% niacinamide.", "If you use it daily, read the label.", "Studies suggest it helps."],
)
def test_rules_ignore_product_descriptions(text: str) -> None:
    assert not rule_hits(POLICY, text)


# ---------------------------------------------------------------------- the guard


async def test_guard_skips_modes_it_does_not_cover() -> None:
    guard = TestimonialGuard(POLICY)
    result = await guard.check(spec_with(["I've used this for a month."], mode="talking_head_explainer"))
    assert not result.applies and result.findings == []


async def test_without_a_classifier_the_rules_decide_and_block(tmp_path: Path) -> None:
    guard = TestimonialGuard(POLICY)
    result = await guard.check(spec_with(["I've used this cream for a month.", "It has SPF 30."]))
    assert result.classifier_status == "skipped"
    (blocking,) = result.blocking
    assert blocking.detail["segment_key"] == "seg_1" and blocking.detail["decided_by"] == "rules"
    assert "rewrite_non_experiential" in blocking.detail["resolutions"]


async def test_the_classifier_clears_a_rule_hit_and_catches_a_miss(tmp_path: Path) -> None:
    provider = fixture_provider(
        tmp_path,
        {
            "items": [
                {"segment_key": "seg_1", "experiential": False, "confidence": 0.9, "reason": "a method, not a product"},
                {"segment_key": "seg_2", "experiential": True, "confidence": 0.85, "claim": "it fixed my breakouts"},
            ]
        },
    )
    guard = TestimonialGuard(POLICY, provider=provider, prompts=PROMPTS)
    spec = spec_with(["I've been using this approach for years.", "Two weeks in, it fixed my breakouts."])
    result = await guard.check(spec, scenario_id="tg_case")
    assert result.classifier_status == "succeeded"
    assert [a.flagged for a in result.assessments] == [False, True]
    (blocking,) = result.blocking
    assert blocking.detail["decided_by"] == "classifier" and blocking.detail["claim"] == "it fixed my breakouts"


async def test_low_confidence_falls_back_to_the_rules(tmp_path: Path) -> None:
    provider = fixture_provider(
        tmp_path, {"items": [{"segment_key": "seg_1", "experiential": False, "confidence": 0.3}]}
    )
    guard = TestimonialGuard(POLICY, provider=provider, prompts=PROMPTS)
    result = await guard.check(spec_with(["I bought this last week."]), scenario_id="tg_case")
    assert result.assessments[0].decided_by == "rules" and result.blocking


async def test_invalid_classifier_output_is_repaired_or_fails_closed(tmp_path: Path) -> None:
    repaired = fixture_provider(
        tmp_path,
        [
            {"raw": "not json"},
            {"items": [{"segment_key": "seg_9", "experiential": False, "confidence": 1.0}]},  # unknown key
            {"items": [{"segment_key": "seg_1", "experiential": True, "confidence": 0.95, "claim": "I bought this"}]},
        ],
    )
    guard = TestimonialGuard(POLICY, provider=repaired, prompts=PROMPTS)
    result = await guard.check(spec_with(["I bought this last week."]), scenario_id="tg_case")
    assert result.classifier_status == "repaired" and result.run is not None and len(result.run.attempts) == 3

    broken = fixture_provider(tmp_path, [{"raw": "x"}, {"raw": "y"}, {"raw": "z"}])
    guard = TestimonialGuard(POLICY, provider=broken, prompts=PROMPTS)
    result = await guard.check(spec_with(["I bought this last week."]), scenario_id="tg_case")
    assert result.classifier_status == "failed" and result.blocking  # rules still block


async def test_a_missing_fixture_means_unavailable_and_the_rules_decide(tmp_path: Path) -> None:
    guard = TestimonialGuard(POLICY, provider=FixtureLLMProvider({"fixtures_dir": str(tmp_path)}), prompts=PROMPTS)
    result = await guard.check(spec_with(["I bought this last week."]), scenario_id="nothing_recorded")
    assert result.classifier_status == "unavailable" and result.blocking


async def test_a_consented_quote_or_a_disclosed_dramatization_resolves_the_claim() -> None:
    guard = TestimonialGuard(POLICY)
    quote = QuoteSource(consent_id=example_spec().version_id, asset_id=example_spec().video_id)
    quoted = spec_with(["I bought this last week."], seg_1={"quote_source": quote})
    result = await guard.check(quoted)
    assert not result.blocking and result.assessments[0].resolution == "quote_source"

    dramatized = spec_with(["I bought this last week."], mode="testimonial_dramatization")
    assert (await guard.check(dramatized)).blocking  # dramatization mode without a disclosure effect
    disclosure = Effect(
        key="fx_disclosure",
        type="disclosure",
        span=SceneSpan(scene_key=dramatized.scenes[0].key),
        params={"text": POLICY.disclosure_text},
    )
    with_disclosure = dramatized.model_copy(update={"effects": [disclosure]})
    result = await guard.check(with_disclosure)
    assert not result.blocking and result.assessments[0].resolution == "dramatization_disclosure"
