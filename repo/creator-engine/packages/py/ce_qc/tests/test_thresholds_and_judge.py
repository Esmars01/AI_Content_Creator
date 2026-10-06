"""Metric thresholds keyed by adapter id and the VLM judge's structured answers (§26)."""

from __future__ import annotations

from pathlib import Path

from ce_config.loader import load_config
from ce_contracts.models import QCMetricResult
from ce_qc.thresholds import judge_metric, metric_score
from ce_qc.vlm_judge import CHECKS, judge_question, judge_schema, parse_judgement

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
DRAFT, FINAL = BUNDLE.qc_tiers["draft"], BUNDLE.qc_tiers["final"]


def result(metric: str, score: float, *, advisory: bool = False) -> QCMetricResult:
    return QCMetricResult(metric=metric, score=score, advisory=advisory)


def test_thresholds_are_keyed_by_the_adapter_that_measured() -> None:
    low = judge_metric(DRAFT, "qc.speech_quality", "dnsmos", result("speech_quality", 2.9))
    assert low.passed is False and low.gating_failure and low.thresholds == {"min_score": 3.0}
    same_score_other_tier = judge_metric(FINAL, "qc.speech_quality", "dnsmos", result("speech_quality", 3.2))
    assert same_score_other_tier.passed is False  # final asks for 3.3
    unknown = judge_metric(DRAFT, "qc.speech_quality", "utmos_v2", result("speech_quality", 1.0))
    assert unknown.passed is None and unknown.advisory and unknown.basis == "no_thresholds"
    assert not unknown.gating_failure


def test_lipsync_is_advisory_and_rated() -> None:
    weak = judge_metric(DRAFT, "qc.lipsync", "syncnet_v1", result("lipsync_score", 2.0))
    assert weak.passed is False and weak.advisory and not weak.gating_failure and weak.rating == "reject"
    ok = judge_metric(DRAFT, "qc.lipsync", "syncnet_v1", result("lipsync_score", 4.0))
    good = judge_metric(DRAFT, "qc.lipsync", "syncnet_v1", result("lipsync_score", 6.0))
    assert (ok.rating, good.rating) == ("acceptable", "good")


def test_beta_languages_only_warn_on_lipsync() -> None:
    verdict = judge_metric(DRAFT, "qc.lipsync", "mock_qc", result("lipsync_score", 1.0), beta_language=True)
    assert verdict.advisory and "beta" in (verdict.reason or "")


def test_metric_score_weights_advisory_metrics_at_half() -> None:
    verdicts = [
        judge_metric(DRAFT, "qc.vqa", "mock_qc", result("visual_quality", 0.8)),
        judge_metric(DRAFT, "qc.lipsync", "mock_qc", result("lipsync_score", 2.0)),
    ]
    assert metric_score(verdicts) == round(1.0 / 1.5, 4)
    assert metric_score([]) == 1.0


def test_the_judge_prompt_names_every_check_and_the_schema_validates_answers() -> None:
    question, schema = judge_question(), judge_schema()
    for check in CHECKS:
        assert check.split("_")[0] in question
    assert schema["properties"]["defects"]["items"]["properties"]["check"]["enum"] == sorted(CHECKS)
    answer = {
        "defects": [
            {"check": "hands", "severity": "critical", "t_s": 1.2, "description": "six fingers"},
            {"check": "nose", "severity": "critical", "t_s": 1.0, "description": "not a check"},
            {"check": "eyes", "severity": "minor", "t_s": "x", "description": "blink"},
            "garbage",
        ]
    }
    judged = parse_judgement(answer, 0.8, min_confidence=0.5)
    assert [d["check"] for d in judged.defects] == ["hands", "eyes"] and judged.critical and judged.counted
    unsure = parse_judgement(answer, 0.3, min_confidence=0.5)
    assert unsure.defects and not unsure.critical and not unsure.counted
    invalid = parse_judgement({"yes": True}, 0.9, min_confidence=0.5)
    assert not invalid.valid and not invalid.critical
