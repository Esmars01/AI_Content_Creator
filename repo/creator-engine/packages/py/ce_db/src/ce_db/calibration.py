"""Measured proxy calibrations per analyzer revision (§16.2, Phase 7), stored as
`model_behavior_profiles` rows with `translator_version = 'proxy_calibration'` (ADR 0045):
`adapter_id` and `revision` name the analyzer, `dimension` the proxy key, `source = 'bench'`,
`measured` the scores (precision, recall, F1, n, reliability, confidence, analyzer capability)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ce_db.models.behavior import ModelBehaviorProfile

__all__ = ["PROFILE_TRANSLATOR", "load_proxy_calibrations", "store_proxy_calibrations"]

PROFILE_TRANSLATOR = "proxy_calibration"


async def store_proxy_calibrations(session: AsyncSession, rows: Iterable[Mapping[str, Any]]) -> int:
    """Upserts rows `{adapter_id, revision, proxy, language?, measured}`; returns the count."""
    count = 0
    for row in rows:
        stmt = insert(ModelBehaviorProfile).values(
            adapter_id=str(row["adapter_id"]),
            translator_version=PROFILE_TRANSLATOR,
            revision=str(row["revision"]),
            dimension=str(row["proxy"]),
            language=str(row.get("language", "und")),
            source="bench",
            measured=dict(row["measured"]),
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=["adapter_id", "translator_version", "revision", "dimension", "language", "source"],
                set_={"measured": stmt.excluded.measured, "updated_at": sa.func.now()},
            )
        )
        count += 1
    return count


async def load_proxy_calibrations(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            sa.select(ModelBehaviorProfile)
            .where(ModelBehaviorProfile.translator_version == PROFILE_TRANSLATOR)
            .order_by(ModelBehaviorProfile.adapter_id, ModelBehaviorProfile.revision, ModelBehaviorProfile.dimension)
        )
    ).scalars()
    return [
        {
            "adapter_id": r.adapter_id,
            "revision": r.revision,
            "dimension": r.dimension,
            "language": r.language,
            "measured": dict(r.measured or {}),
        }
        for r in rows
    ]
