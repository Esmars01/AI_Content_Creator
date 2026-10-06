"""GPUProvider interface and registry (§25). Implementations are plugins (`plugins/providers/gpu/*`).

Status: implemented and tested in Phase 2 — the interface (provision, start, stop, terminate,
status, list_offers, price, health) and the entry-point registry; the `mock` provider plugin
simulates workers with configurable prices, start latency and failure injection. `local_docker`
and the RunPod providers arrive in Phase 9.
"""

from ce_gpu.provider import (
    GPUOffer,
    GPUProvider,
    NoCapacityError,
    ProviderError,
    ProviderInstance,
    ProvisionSpec,
    create_gpu_provider,
    gpu_provider_keys,
)

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2

__all__ = [
    "GPUOffer",
    "GPUProvider",
    "NoCapacityError",
    "ProviderError",
    "ProviderInstance",
    "ProvisionSpec",
    "create_gpu_provider",
    "gpu_provider_keys",
]
