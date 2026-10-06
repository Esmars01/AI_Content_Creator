"""Every installed adapter plugin passes the adapter contract suite (§37): the mocks, and the real
CPU engines on their fetched assets — the CPU smoke tests that back `validation: smoke_passed`
(§40 Phase 7). Engines whose assets are missing skip with the reason."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ce_contracts.contract_suite import check_adapter
from ce_contracts.manifest import missing_assets
from ce_contracts.plugins import discover

REGISTRY = discover(app_env="test", include_mocks=True)
ADAPTERS = sorted(p.id for p in REGISTRY.plugins.values() if p.manifest.kind != "provider")
MODEL_CACHE = Path(os.environ.get("CE_TEST_MODEL_CACHE", Path(__file__).resolve().parents[3] / ".cache" / "models"))


def test_every_plugin_loaded_without_rejection() -> None:
    assert REGISTRY.rejected == []
    assert {"mock_avatar_global", "mock_avatar_segment", "mock_observer", "mock_voice"} <= set(ADAPTERS)


@pytest.mark.parametrize("plugin_id", ADAPTERS)
def test_adapter_contract(plugin_id: str, tmp_path: Path) -> None:
    plugin = REGISTRY.get(plugin_id)
    missing = missing_assets(plugin.manifest, MODEL_CACHE)
    if missing:
        message = f"{plugin_id}: assets not fetched ({', '.join(missing[:2])}); run `make fetch-cpu-assets`"
        if os.environ.get("CE_REQUIRE_CPU_ASSETS"):  # the CI smoke job: a missing asset is a failure
            pytest.fail(message)
        pytest.skip(message)
    check_adapter(plugin, tmp_path, model_cache_dir=MODEL_CACHE if plugin.manifest.assets else None)


def test_real_cpu_engines_are_smoke_validated() -> None:
    """A real CPU engine claims `smoke_passed` only because the contract above runs it (rule 5)."""
    for plugin in REGISTRY.plugins.values():
        if plugin.manifest.cpu_engine:
            assert plugin.manifest.validation == "smoke_passed", plugin.id
