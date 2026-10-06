"""The smoke/bench harness (Phase 8, `ce_worker.validation`): the golden set, case selection,
checks, measurements, and rule 5 at the recording boundary (stand-in runs are never evidence)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ce_contracts.plugins import discover

from ce_worker.validation import (
    StandInReportError,
    applicable_cases,
    assert_recordable,
    character_error_rate,
    load_smoke_set,
    run_validation,
    word_error_rate,
)
from ce_worker.validation_cli import main

ROOT = Path(__file__).resolve().parents[4]
CASES = ROOT / "eval" / "smoke" / "cases.yaml"
REGISTRY = discover(app_env=None, include_mocks=True)
REAL_CACHE = ROOT / ".cache" / "models"


def test_the_smoke_set_loads_and_its_media_match_their_pins() -> None:
    smoke = load_smoke_set(CASES)
    assert smoke.version == "smoke-v1"
    assert smoke.verify_media() == []
    assert len({c.id for c in smoke.cases}) == len(smoke.cases)
    for case in smoke.cases:  # every behavior fixture and media reference resolves
        text = json.dumps(case.request)
        for name in smoke.media:
            text = text.replace(f'"${name}"', "")
        assert '"$' not in text.replace('"$ref:', ""), case.id


def test_every_sandbox_adapter_has_a_case_for_every_capability() -> None:
    """Otherwise no smoke run of it could ever pass (§24: smoke evidence before promotion)."""
    smoke = load_smoke_set(CASES)
    for plugin in REGISTRY.plugins.values():
        manifest = plugin.manifest
        if manifest.kind == "provider" or manifest.status != "sandbox":
            continue
        run, _skipped, missing = applicable_cases(manifest, smoke)
        assert run and not missing, f"{manifest.id}: no case for {missing}"


def test_language_filter_and_missing_capabilities() -> None:
    smoke = load_smoke_set(CASES)
    run, skipped, _ = applicable_cases(REGISTRY.get("ctc_aligner").manifest, smoke)
    assert {c.id for c in run} == {"align_tr", "align_ar"}
    assert any(s.startswith("align_en") for s in skipped)


def test_error_rates() -> None:
    assert word_error_rate("The quick, brown fox.", "the quick brown fox") == 0.0
    assert word_error_rate("a b c d", "a x c") == 0.5
    assert character_error_rate("abc", "abd") == pytest.approx(1 / 3)


def test_stand_in_and_inconsistent_reports_are_never_recordable() -> None:
    real = {"kind": "smoke", "backend": "real", "verdict": "smoke_passed", "cases": [{"passed": True}]}
    assert_recordable(real)
    assert_recordable({**real, "verdict": "failed", "cases": [{"passed": False}]})
    for bad in (
        {**real, "backend": "test"},
        {**real, "verdict": "stand_in_only"},
        {**real, "cases": [{"passed": False}]},
        {**real, "cases": []},
        {**real, "missing_capabilities": ["voice.design"]},
    ):
        with pytest.raises(StandInReportError):
            assert_recordable(bad)


async def test_stand_in_run_of_a_gpu_adapter_exercises_the_adapter_but_claims_nothing(tmp_path: Path) -> None:
    smoke = load_smoke_set(CASES)
    report = await run_validation(
        REGISTRY.get("wan22_ti2v_5b"), smoke, backend="test", work=tmp_path, model_cache_dir=tmp_path / "cache"
    )
    assert report["backend"] == "test" and report["verdict"] == "stand_in_only"
    assert {c["case_id"] for c in report["cases"]} == {"broll_t2v", "broll_i2v"}
    assert all(c["passed"] for c in report["cases"]), report["cases"]
    with pytest.raises(StandInReportError):
        assert_recordable(report)


async def test_bench_mode_reports_timings_and_stays_pending(tmp_path: Path) -> None:
    smoke = load_smoke_set(CASES)
    report = await run_validation(
        REGISTRY.get("mock_voice"), smoke, mode="bench", backend="real", work=tmp_path,
        model_cache_dir=tmp_path / "cache", seeds=(1, 2), case_ids=["tts_en"],
    )  # fmt: skip
    assert report["verdict"] == "pending"
    assert report["timings"]["tts_en"]["n"] == 2
    (first, _second) = report["cases"]
    assert first["measurements"]["pauses"]["measured"] is True


def test_cli_dry_run_writes_the_report(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    code = main("smoke", ["rife_425", "--backend", "test", "--out", str(out), "--model-cache", str(tmp_path)])
    report = json.loads(out.read_text())
    assert code == 0 and report["verdict"] == "stand_in_only" and report["adapter_id"] == "rife_425"


def test_cli_refuses_to_record_a_stand_in_run(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    with pytest.raises(StandInReportError):
        main("smoke", ["rife_425", "--backend", "test", "--out", str(out), "--record", "--api-key", "ce_key_x"])


@pytest.mark.slow
async def test_real_cpu_engine_passes_the_smoke_set(tmp_path: Path) -> None:
    """The harness on real weights: faster-whisper (CPU) transcribes, identifies and aligns the
    reference speech. Skips without `make fetch-cpu-assets`."""
    if not (REAL_CACHE / "faster-whisper-base").exists() and not any(REAL_CACHE.glob("**/faster-whisper-base")):
        pytest.skip("CPU engine assets are not fetched (make fetch-cpu-assets)")
    smoke = load_smoke_set(CASES)
    report = await run_validation(
        REGISTRY.get("faster_whisper_cpu"), smoke, backend="real", work=tmp_path, model_cache_dir=REAL_CACHE
    )
    assert report["verdict"] == "smoke_passed", report["cases"]
    assert_recordable(report)


def test_family_image_entrypoint_refuses_misconfiguration() -> None:
    """`python -m ce_worker` (GPU family images): env-only config, loud refusals."""
    from ce_worker.__main__ import config_from_env, registry_from_env, startup_errors

    env = {"APP_ENV": "prod", "WORKER_RUNTIME_FAMILY": "wan", "WORKER_TOKEN": "t" * 32, "WORKER_VRAM_GB": "48"}
    registry = registry_from_env(env)
    assert {"infinitetalk", "longcat_avatar"} <= set(registry.plugins)
    assert startup_errors(env, registry) == []
    narrowed = registry_from_env({**env, "WORKER_ADAPTERS": "infinitetalk"})
    assert set(narrowed.plugins) == {"infinitetalk"}
    config = config_from_env(env)
    assert (config.runtime_family, config.vram_gb, config.app_env) == ("wan", 48.0, "prod")
    assert any("MOCK_GPU" in e for e in startup_errors({**env, "MOCK_GPU": "true"}, registry))
    assert any("WORKER_TOKEN" in e for e in startup_errors({**env, "WORKER_TOKEN": ""}, registry))
    empty = registry_from_env({**env, "WORKER_RUNTIME_FAMILY": "no_such_family"})
    assert any("no plugin" in e for e in startup_errors(env, empty))
