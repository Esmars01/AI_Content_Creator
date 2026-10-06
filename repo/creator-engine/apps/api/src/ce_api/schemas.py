"""Base classes for request and response bodies."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = ["Body", "Out", "examples"]


class Body(BaseModel):
    """Request bodies: unknown fields are rejected, so typos never pass silently."""

    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    """Response bodies, readable from ORM rows."""

    model_config = ConfigDict(from_attributes=True)


def examples(items: list[Any]) -> ConfigDict:
    """`model_config = examples([...])`: request examples shown in OpenAPI (and checked by its tests)."""
    return ConfigDict(json_schema_extra={"examples": items})
