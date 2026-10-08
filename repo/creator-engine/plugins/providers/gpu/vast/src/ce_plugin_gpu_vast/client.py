"""A small async client of the Vast.ai REST API (`https://console.vast.ai/api/v0`).

The endpoints and body shapes follow Vast's own Python client, `vastai` 1.8.3 (PyPI, repository
github.com/vast-ai/vast-cli), read 2026-10-07: `vastai/api/offers.py`, `vastai/api/instances.py`
and `vastai/data/*.py`. `tests/vast_schema.json` records the field lists taken from those sources,
and the tests check every request body against it. No live call has been made (no key, no approved
spend).

Errors map onto the `GPUProvider` vocabulary:
- An offer that is gone when it is rented (a 4xx answer saying it is not available) is `NoCapacityError`.
  The provider then tries its next offer, and the fleet manager falls back to the next GPU class or
  provider (§25).
- Anything else is `ProviderError`.

A rental is never retried after a 5xx or a timeout: it may have gone through, and a retry could rent
a second machine. The API key never appears in an error message or a log line, and neither does the
body of `GET /users/current`, which contains it.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx
from ce_gpu.provider import NoCapacityError, ProviderError, ProvisionOutcomeUnknown

__all__ = ["DEFAULT_BASE_URL", "OfferUnavailableError", "VastClient", "api_key_from"]

DEFAULT_BASE_URL = "https://console.vast.ai"
# Vast publishes no error schema for an ask that was rented by someone else between search and rent
# (see the module docstring). These are the wordings its API uses for an unavailable offer; anything
# else stays a ProviderError, which is the safe side (no blind retry of a rental).
_UNAVAILABLE = re.compile(
    r"(no_such_ask|not available|no longer available|unavailable|already rented|is rented|not rentable|"
    r"insufficient (capacity|resources)|no (gpus?|machines?) available)",
    re.I,
)


class OfferUnavailableError(NoCapacityError):
    """The offer cannot be rented any more; another offer of the same search may still be free."""


def api_key_from(config: dict[str, Any]) -> str | None:
    """`config["api_key"]` (from the provider row's `credentials_ref`) or `VAST_API_KEY`."""
    return str(config.get("api_key") or os.environ.get("VAST_API_KEY") or "") or None


def _api_message(response: httpx.Response) -> str:
    """The error text of a Vast answer (`msg`, `error` or the raw body), shortened."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(body, dict):
        parts = [str(body[k]) for k in ("error", "msg", "message", "detail") if body.get(k)]
        if parts:
            return " / ".join(parts)[:300]
    return response.text[:300]


class VastClient:
    def __init__(
        self,
        api_key: str | None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.timeout_s = timeout_s

    def _http(self) -> httpx.AsyncClient:
        if not self.api_key:
            raise ProviderError("Vast: no API key configured (VAST_API_KEY or the provider's credentials)")
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
            timeout=self.timeout_s,
            transport=self.transport,
        )

    async def request(
        self, method: str, path: str, *, json: Any = None, params: Any = None, rental: bool = False
    ) -> Any:
        async with self._http() as http:
            try:
                response = await http.request(method, path, json=json, params=params)
            except httpx.TimeoutException:
                if rental:
                    raise ProvisionOutcomeUnknown(
                        f"Vast {method} {path}: timeout (the rental may have gone through: check the account "
                        "before retrying)"
                    ) from None
                raise ProviderError(f"Vast {method} {path}: timeout") from None
            except httpx.HTTPError as exc:
                raise ProviderError(f"Vast {method} {path}: {type(exc).__name__}") from None
        status = response.status_code
        if status in (401, 403):
            raise ProviderError(f"Vast {method} {path}: HTTP {status} (the API key was refused)")
        if status >= 400:
            message = _api_message(response)
            if status < 500 and _UNAVAILABLE.search(message):
                raise OfferUnavailableError(f"Vast {method} {path}: offer not available ({status}): {message}")
            if rental and status >= 500:
                raise ProvisionOutcomeUnknown(f"Vast {method} {path}: HTTP {status} (the rental may have gone through)")
            raise ProviderError(f"Vast {method} {path}: HTTP {status}: {message}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ProviderError(f"Vast {method} {path}: the answer is not JSON") from None

    @staticmethod
    def _checked(path: str, answer: Any) -> dict[str, Any]:
        """A `{"success": ...}` answer; `success: false` is an error with Vast's message."""
        if not isinstance(answer, dict):
            raise ProviderError(f"Vast {path}: unexpected answer {type(answer).__name__}")
        if answer.get("success") is False:
            message = str(answer.get("msg") or answer.get("error") or "refused")[:300]
            if _UNAVAILABLE.search(message):
                raise OfferUnavailableError(f"Vast {path}: offer not available: {message}")
            raise ProviderError(f"Vast {path}: {message}")
        return answer

    # offers
    async def search_offers(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        answer = await self.request("POST", "/api/v0/bundles/", json=query)
        offers = answer.get("offers") if isinstance(answer, dict) else None
        if not isinstance(offers, list):
            raise ProviderError("Vast POST /api/v0/bundles/: the answer has no `offers` list")
        return [o for o in offers if isinstance(o, dict)]

    # instances
    async def create_instance(self, offer_id: int, body: dict[str, Any]) -> int:
        path = f"/api/v0/asks/{offer_id}/"
        answer = self._checked(path, await self.request("PUT", path, json=body, rental=True))
        contract = answer.get("new_contract")
        if not isinstance(contract, int) or isinstance(contract, bool):
            # the rental may exist: say so instead of retrying (the API key of the instance is never kept)
            raise ProviderError(f"Vast PUT {path}: no instance id (`new_contract`) in the answer; check the account")
        return contract

    async def get_instance(self, instance_id: str) -> dict[str, Any] | None:
        """The instance, or None when Vast no longer knows it (destroyed)."""
        try:
            answer = await self.request("GET", f"/api/v0/instances/{instance_id}/", params={"owner": "me"})
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        if not isinstance(answer, dict) or "instances" not in answer:
            raise ProviderError(f"Vast GET /api/v0/instances/{instance_id}/: the answer has no `instances`")
        row = answer["instances"]
        if row is None:
            return None
        if not isinstance(row, dict):
            raise ProviderError(f"Vast GET /api/v0/instances/{instance_id}/: `instances` is not an object")
        return row

    async def list_instances(self, *, page_limit: int = 25, max_pages: int = 40) -> list[dict[str, Any]]:
        """Every instance of the account: `GET /api/v1/instances/`, paged by `next_token` (vastai 1.8.3
        `show instances`; the v0 list is deprecated). Query values are JSON, as the official client sends."""
        params: dict[str, Any] = {"select_filters": {}, "order_by": [{"col": "id", "dir": "asc"}], "limit": page_limit}
        rows: list[dict[str, Any]] = []
        for _ in range(max_pages):
            query = {k: v if isinstance(v, str) else json.dumps(v, separators=(",", ":")) for k, v in params.items()}
            answer = await self.request("GET", "/api/v1/instances/", params=query)
            if not isinstance(answer, dict) or not isinstance(answer.get("instances", []), list):
                raise ProviderError("Vast GET /api/v1/instances/: the answer has no `instances` list")
            rows += [r for r in answer.get("instances") or [] if isinstance(r, dict)]
            token = answer.get("next_token")
            if not token:
                return rows
            params["after_token"] = token
        raise ProviderError(f"Vast GET /api/v1/instances/: more than {max_pages} pages")

    async def reboot_instance(self, instance_id: str) -> None:
        """Restarts the container on the same machine, keeping its GPU (`PUT /instances/reboot/{id}/`)."""
        path = f"/api/v0/instances/reboot/{instance_id}/"
        self._checked(path, await self.request("PUT", path, json={}))

    async def set_state(self, instance_id: str, state: str) -> None:
        path = f"/api/v0/instances/{instance_id}/"
        self._checked(path, await self.request("PUT", path, json={"state": state}))

    async def destroy_instance(self, instance_id: str) -> bool:
        """True when destroyed now, False when Vast had no such instance (already gone)."""
        path = f"/api/v0/instances/{instance_id}/"
        try:
            answer = await self.request("DELETE", path, json={})
        except ProviderError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise
        self._checked(path, answer if answer is not None else {"success": True})
        return True

    async def current_user(self) -> None:
        """A cheap authenticated read for health. The body (it carries the account's key) is discarded."""
        await self.request("GET", "/api/v0/users/current")
