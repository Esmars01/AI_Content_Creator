"""I11 — Provenance is always on in production: production refuses to start with a mock
watermarker or signer or with `PROVENANCE_MODE=mock_dev`; dev renders in mock_dev mode carry the
burned "MOCK PROVENANCE — NOT FOR DISTRIBUTION" label and are not exportable."""

from __future__ import annotations

from pathlib import Path

import pytest
from ce_config.settings import load_effective, startup_issues
from ce_policy import MOCK_LABEL, ProvenanceRefused, burned_labels, require_provenance_mode

pytestmark = [pytest.mark.invariant]

ROOT = Path(__file__).resolve().parents[2]
PROD = {
    "APP_ENV": "prod",
    "SECRET_KEY": "x" * 48,
    "WORKER_TOKEN": "w" * 40,
    "COOKIE_SECURE": "true",
    "MOCK_GPU": "false",
    "LLM_PROVIDER": "anthropic",
    "STORAGE_PROVIDER": "s3",
    "DATABASE_URL": "postgresql+asyncpg://localhost/none",
}


def test_production_config_refuses_mock_dev() -> None:
    effective = load_effective(ROOT / "config", {**PROD, "PROVENANCE_MODE": "mock_dev"})
    codes = {i.code for i in startup_issues(effective) if i.severity == "error"}
    assert "provenance" in codes


def test_production_default_is_real_provenance() -> None:
    effective = load_effective(ROOT / "config", PROD)
    assert effective.provenance_mode == "real"
    assert not [i for i in startup_issues(effective) if i.code == "provenance"]


def test_every_service_refuses_to_start_with_mock_provenance_in_production() -> None:
    from ce_orchestrator.worker import refuse_if_misconfigured
    from ce_scheduler.app import create_app

    effective = load_effective(ROOT / "config", {**PROD, "PROVENANCE_MODE": "mock_dev"})
    with pytest.raises(RuntimeError, match="provenance"):
        create_app(effective)
    with pytest.raises(RuntimeError, match="provenance"):
        refuse_if_misconfigured(effective)


def test_production_refuses_without_real_provenance_adapters() -> None:
    """The VideoSeal/AudioSeal adapters (Phase 8) are `sandbox` / `untested_on_gpu`: they exist but
    are not production-routable, so they cannot satisfy I11 until a smoke run promotes them."""
    from ce_exec.context import build_services
    from ce_orchestrator.worker import refuse_if_misconfigured

    effective = load_effective(ROOT / "config", PROD)
    services = build_services(effective, events=False, pool_size=1)
    assert services.registry.by_capability("provenance.watermark_video")  # the sandbox adapter is installed
    with pytest.raises(RuntimeError, match=r"no adapter for provenance\.watermark_video is production-routable"):
        refuse_if_misconfigured(effective, services)
    assert not any(p.manifest.mock for p in services.registry.plugins.values())  # MOCK_GPU=false: no mocks


def test_production_starts_once_the_watermarkers_are_promoted() -> None:
    """A registry overlay that records a smoke promotion (ce_db.registry) makes the same adapters
    production-routable; a disabled one never counts."""
    import dataclasses

    from ce_exec.context import build_services
    from ce_orchestrator.worker import refuse_if_misconfigured

    effective = load_effective(ROOT / "config", PROD)
    services = build_services(effective, events=False, pool_size=1)
    caps = ("provenance.watermark_video", "provenance.watermark_audio", "provenance.sign")
    ids = {p.id for cap in caps for p in services.registry.by_capability(cap)}
    manifests = dict(services.catalog.manifests)
    for adapter_id in ids:
        manifests[adapter_id] = manifests[adapter_id].model_copy(
            update={"status": "production", "validation": "smoke_passed"}
        )
    services.catalog = dataclasses.replace(services.catalog, manifests=manifests)
    refuse_if_misconfigured(effective, services)  # does not raise
    video = {p.id for p in services.registry.by_capability("provenance.watermark_video")}
    services.catalog = dataclasses.replace(services.catalog, disabled=frozenset(video))
    with pytest.raises(RuntimeError, match="watermark_video"):
        refuse_if_misconfigured(effective, services)


def test_mock_provenance_mode_is_dev_and_test_only() -> None:
    assert require_provenance_mode("mock_dev", "dev") == "mock_dev"
    with pytest.raises(ProvenanceRefused):
        require_provenance_mode("mock_dev", "prod")
    assert burned_labels("mock_dev", False) == [MOCK_LABEL]
    assert burned_labels("real", False) == []


def test_gpu_worker_refuses_mock_provenance_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from ce_gpu_worker.__main__ import _run

    for key, value in {**PROD, "PROVENANCE_MODE": "mock_dev", "CE_CONFIG_ROOT": str(ROOT / "config")}.items():
        monkeypatch.setenv(key, value)
    assert asyncio.run(_run()) == 2
