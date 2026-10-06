"""The local provider refuses lifecycle calls and lists informational offers."""

from __future__ import annotations

import asyncio

import pytest
from ce_gpu.provider import ProviderError, ProvisionSpec, create_gpu_provider


def test_registered_without_mocks_and_refuses_provisioning() -> None:
    provider = create_gpu_provider("local", app_env="prod", include_mocks=False)
    offers = asyncio.run(provider.list_offers())
    assert [(o.gpu_class, o.price_per_hour_usd, o.available) for o in offers] == [("cpu", 0.0, 0)]
    with pytest.raises(ProviderError, match="started by the operator"):
        asyncio.run(provider.provision(ProvisionSpec(gpu_class="cpu", region="local", runtime_family="cpu_model")))
    assert asyncio.run(provider.health()).ok
