"""A small async client of RunPod's REST API v1 (`https://rest.runpod.io/v1`, OpenAPI 0.1.0 read
2026-10-04; the request shapes are checked against that schema in the tests).

Errors map onto the `GPUProvider` vocabulary: an answer that says no machine matches (RunPod returns a
4xx/5xx with "no ... available" wording) is `NoCapacityError` — the fleet manager then falls back to the
next GPU class or provider (§25); anything else is `ProviderError`. The API key never appears in an
error message or a log line.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
from ce_gpu.provider import NoCapacityError, ProviderError

__all__ = ["RunPodClient", "api_key_from"]

DEFAULT_BASE_URL = "https://rest.runpod.io/v1"
_CAPACITY = re.compile(
    r"(no (longer )?(any )?(instances?|machines?|gpus?)[^.]*available|out of stock|insufficient capacity)", re.I
)


def api_key_from(config: dict[str, Any]) -> str | None:
    """`config["api_key"]` (from the provider's credentials reference) or `RUNPOD_API_KEY`."""
    return str(config.get("api_key") or os.environ.get("RUNPOD_API_KEY") or "") or None


class RunPodClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout_s = timeout_s

    def _http(self) -> httpx.AsyncClient:
        if not self.api_key:
            raise ProviderError("RunPod: no API key configured (RUNPOD_API_KEY or the provider's credentials)")
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout_s,
            transport=self.transport,
        )

    async def request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        async with self._http() as http:
            try:
                response = await http.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                raise ProviderError(f"RunPod {method} {path}: {type(exc).__name__}") from None
        if response.status_code >= 400:
            text = response.text[:500]
            if _CAPACITY.search(text):
                raise NoCapacityError(f"RunPod {method} {path}: no capacity ({response.status_code})")
            raise ProviderError(f"RunPod {method} {path}: HTTP {response.status_code}: {text}")
        if not response.content:
            return None
        return response.json()

    # pods
    async def create_pod(self, body: dict[str, Any]) -> dict[str, Any]:
        return dict(await self.request("POST", "/pods", json=body))

    async def get_pod(self, pod_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", f"/pods/{pod_id}"))

    async def list_pods(self) -> list[dict[str, Any]]:
        return list(await self.request("GET", "/pods") or [])

    async def start_pod(self, pod_id: str) -> None:
        await self.request("POST", f"/pods/{pod_id}/start")

    async def stop_pod(self, pod_id: str) -> None:
        await self.request("POST", f"/pods/{pod_id}/stop")

    async def delete_pod(self, pod_id: str) -> None:
        await self.request("DELETE", f"/pods/{pod_id}")

    # serverless endpoints
    async def get_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        return dict(await self.request("GET", f"/endpoints/{endpoint_id}"))

    async def update_endpoint(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return dict(await self.request("PATCH", f"/endpoints/{endpoint_id}", json=body) or {})
