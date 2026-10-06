"""The API's client of the scheduler's internal fleet endpoints (Phase 9).

The scheduler is the fleet's only authority (§25): provider plugins are installed and configured
there, so offers, provisioning and stopping go through it. The API authenticates with a token both
services derive from `SECRET_KEY` (`ce_scheduler.app.admin_token`); it is never given to workers.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx
from ce_core.errors import ConflictError, InvalidInputError, Issue, NotFoundError, UpstreamUnavailableError

__all__ = ["SchedulerClient", "fleet_admin_token"]


def fleet_admin_token(secret_key: str) -> str:
    return hmac.new(secret_key.encode(), b"ce-scheduler-fleet-admin", hashlib.sha256).hexdigest()


class SchedulerClient:
    def __init__(
        self,
        base_url: str,
        secret_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = fleet_admin_token(secret_key)
        self.transport = transport
        self.timeout_s = timeout_s

    async def _call(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
            ) as http:
                response = await http.request(
                    method,
                    f"/internal/v1/admin/fleet{path}",
                    json=json,
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    headers={"X-Admin-Token": self.token},
                )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError(f"the scheduler did not answer ({type(exc).__name__})") from None
        detail = ""
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", ""))
            except ValueError:
                detail = response.text[:300]
        if response.status_code == 404:
            raise NotFoundError(detail or "not found")
        if response.status_code == 409:
            raise ConflictError(detail or "conflict", issues=[Issue("fleet", detail)])
        if response.status_code == 422:
            raise InvalidInputError(detail or "invalid", issues=[Issue("fleet", detail)])
        if response.status_code >= 400:
            raise UpstreamUnavailableError(f"the scheduler answered {response.status_code}: {detail}")
        return response.json() if response.content else None

    async def status(self) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("GET", "/status")
        return result

    async def offers(self, gpu_class: str | None = None, region: str | None = None) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/offers", params={"gpu_class": gpu_class, "region": region}))

    async def registered(self) -> list[dict[str, Any]]:
        return list(await self._call("GET", "/registered"))

    async def reload(self) -> None:
        await self._call("POST", "/reload")

    async def provision(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("POST", "/provision", json=body)
        return result

    async def stop(self, worker_id: str, action: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._call("POST", f"/workers/{worker_id}/stop", json={"action": action})
        return result
