"""Base class for every spec, DNA and behavior model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

__all__ = ["SpecModel"]


class SpecModel(BaseModel):
    """Strict, immutable Pydantic model.

    Unknown fields are rejected (a typo must never silently drop a requested behavior),
    instances are frozen (specs are immutable per version; edits produce new specs through
    `EditOperation`), and enum values serialize as their string values.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        use_enum_values=True,
        validate_default=True,
        ser_json_inf_nan="strings",
    )
