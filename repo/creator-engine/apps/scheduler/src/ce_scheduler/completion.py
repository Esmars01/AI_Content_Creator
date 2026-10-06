"""Temporal ↔ scheduler handoff (§25): the orchestrator's dispatch activity stores its task token
in the GPU task's payload and completes asynchronously; the scheduler completes, fails or
heartbeats it through the client's async activity handle.

`ActivityCompleter` abstracts that so the scheduler's tests run without a Temporal server.
"""

from __future__ import annotations

import base64
from typing import Any, Protocol

__all__ = ["ActivityCompleter", "Cancelled", "Gone", "TemporalCompleter", "decode_token", "encode_token"]


class Cancelled(Exception):
    """The workflow cancelled the activity (heartbeat reported cancellation)."""


class Gone(Exception):
    """The activity no longer exists (its workflow ended, or it already completed)."""


def encode_token(token: bytes) -> str:
    return base64.b64encode(token).decode("ascii")


def decode_token(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


class ActivityCompleter(Protocol):
    async def complete(self, token: str, result: dict[str, Any]) -> None: ...

    async def fail(self, token: str, error_class: str, message: str) -> None: ...

    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None:
        """Raises `Cancelled` when cancellation was requested, `Gone` when the activity is gone."""

    async def report_cancellation(self, token: str) -> None: ...


class TemporalCompleter:
    """Completes activities through `temporalio.client.Client.get_async_activity_handle`."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def _handle(self, token: str) -> Any:
        return self.client.get_async_activity_handle(task_token=decode_token(token))

    async def complete(self, token: str, result: dict[str, Any]) -> None:
        from temporalio.service import RPCError

        try:
            await self._handle(token).complete(result)
        except RPCError as exc:
            raise Gone(str(exc)) from exc

    async def fail(self, token: str, error_class: str, message: str) -> None:
        from temporalio.exceptions import ApplicationError
        from temporalio.service import RPCError

        error = ApplicationError(
            message or error_class, {"error_class": error_class}, type=error_class, non_retryable=True
        )
        try:
            await self._handle(token).fail(error)
        except RPCError as exc:
            raise Gone(str(exc)) from exc

    async def heartbeat(self, token: str, details: dict[str, Any] | None = None) -> None:
        from temporalio.client import AsyncActivityCancelledError
        from temporalio.service import RPCError

        try:
            if details is None:
                await self._handle(token).heartbeat()
            else:
                await self._handle(token).heartbeat(details)
        except AsyncActivityCancelledError as exc:
            raise Cancelled(str(exc)) from exc
        except RPCError as exc:
            raise Gone(str(exc)) from exc

    async def report_cancellation(self, token: str) -> None:
        from temporalio.service import RPCError

        try:
            await self._handle(token).report_cancellation()
        except RPCError as exc:
            raise Gone(str(exc)) from exc
