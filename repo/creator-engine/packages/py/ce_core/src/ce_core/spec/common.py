"""Spec parts shared by several sections: traceability (`derived_from`), asset references, effects."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from ce_core.enums import DerivedFromKind
from ce_core.keys import AssetKey, EffectKey
from ce_core.scalars import Digest, NonEmptyStr, Token
from ce_core.spec.anchors import TimeSpan
from ce_core.spec.base import SpecModel
from ce_core.spec.paths import SpecPathStr

__all__ = ["AssetRef", "DerivedFrom", "Effect"]


class DerivedFrom(SpecModel):
    """Why a decision was made (§11 traceability). `compiler_approximation` entries carry the planned route's digest."""

    kind: DerivedFromKind
    ref: SpecPathStr
    route_digest: Digest | None = None

    @model_validator(mode="after")
    def _route_digest_only_for_approximations(self) -> DerivedFrom:
        if self.kind == DerivedFromKind.COMPILER_APPROXIMATION and self.route_digest is None:
            raise ValueError("derived_from of kind compiler_approximation needs the planned route_digest (§15.7)")
        if self.kind != DerivedFromKind.COMPILER_APPROXIMATION and self.route_digest is not None:
            raise ValueError("route_digest is only recorded for compiler_approximation entries")
        return self


class AssetRef(SpecModel):
    """A user asset used by the spec (B-roll, screen recordings, logos, reference frames)."""

    key: AssetKey
    asset_id: UUID
    role: Token
    label: str = ""


class Effect(SpecModel):
    """A deterministic effect plugin applied over a span (`fx_` key; transitions, titles, lower thirds…)."""

    key: EffectKey
    type: Token
    span: TimeSpan
    params: dict[str, Any] = Field(default_factory=dict)
    derived_from: list[DerivedFrom] = Field(default_factory=list)
    label: NonEmptyStr | None = None
