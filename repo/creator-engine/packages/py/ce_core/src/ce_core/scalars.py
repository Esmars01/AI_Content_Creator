"""Common annotated scalar types for spec, DNA and behavior models."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints

__all__ = [
    "ASPECTS",
    "Aspect",
    "Digest",
    "Hz",
    "LanguageTag",
    "NonEmptyStr",
    "Position3",
    "SignedUnit",
    "Token",
    "Unit",
]

Unit = Annotated[float, Field(ge=0.0, le=1.0)]
"""A scalar in 0..1 (intensities, ratios, DNA scales)."""

SignedUnit = Annotated[float, Field(ge=-1.0, le=1.0)]
"""A scalar in -1..1 (deltas)."""

_BCP47 = re.compile(r"^[a-z]{2,3}(?:-[A-Z][a-z]{3})?(?:-(?:[A-Z]{2}|\d{3}))?$")


def _bcp47(value: str) -> str:
    if not _BCP47.match(value):
        raise ValueError(f"{value!r} is not a BCP-47 language tag of the form ll, ll-RR or ll-Ssss-RR")
    return value


LanguageTag = Annotated[str, AfterValidator(_bcp47)]
"""BCP-47 language tag (`en`, `en-US`, `tr-TR`, `az-Latn-AZ`, `ar`)."""

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
"""A `sha256:<64 hex>` content digest."""

Token = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]+(?:_[a-z0-9]+)*$", max_length=64)]
"""A vocabulary token shape (membership is checked against the declared vocab_version)."""

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]

Position3 = tuple[Unit, Unit, Annotated[float, Field(ge=0.0)]]
"""Normalized floor-plan position: x, y in 0..1, z height in metres (§19.1)."""

Hz = Annotated[float, Field(gt=0.0)]

ASPECTS = ("9:16", "16:9", "1:1", "4:5")
Aspect = Annotated[str, StringConstraints(pattern=r"^(9:16|16:9|1:1|4:5)$")]
