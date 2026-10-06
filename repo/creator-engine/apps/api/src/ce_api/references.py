"""Reference checks shared by identity endpoints: assets and versions an approval relies on."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import Issue
from ce_db.models.assets import Asset
from sqlalchemy.ext.asyncio import AsyncSession

from ce_api.uploads.validation import family_of

__all__ = ["asset_issues", "version_issues"]


async def asset_issues(
    session: AsyncSession, org_id: UUID, asset_ids: Iterable[UUID], *, path: str, family: str | None = None
) -> list[Issue]:
    ids = list(dict.fromkeys(asset_ids))
    if not ids:
        return []
    rows = {
        a.id: a
        for a in (await session.execute(sa.select(Asset).where(Asset.org_id == org_id, Asset.id.in_(ids)))).scalars()
    }
    issues: list[Issue] = []
    for asset_id in ids:
        asset = rows.get(asset_id)
        if asset is None:
            issues.append(
                Issue(
                    "asset_missing",
                    "asset not found in this organization",
                    path=path,
                    detail={"asset_id": str(asset_id)},
                )
            )
        elif asset.status != "ready":
            issues.append(
                Issue("asset_not_ready", f"asset is {asset.status}", path=path, detail={"asset_id": str(asset_id)})
            )
        elif family is not None and family_of(asset.mime) != family:
            issues.append(
                Issue(
                    "asset_media_type",
                    f"asset must be an {family}",
                    path=path,
                    detail={"asset_id": str(asset_id), "mime": asset.mime},
                )
            )
    return issues


async def version_issues(
    session: AsyncSession,
    model: Any,
    org_id: UUID,
    version_id: UUID | None,
    *,
    path: str,
    what: str,
    required: bool = True,
) -> list[Issue]:
    """The referenced version exists in the org and is approved."""
    if version_id is None:
        return [Issue("reference_missing", f"an approved {what} is required", path=path)] if required else []
    status = (
        await session.execute(sa.select(model.status).where(model.org_id == org_id, model.id == version_id))
    ).scalar_one_or_none()
    if status is None:
        return [Issue("reference_missing", f"{what} not found", path=path, detail={"id": str(version_id)})]
    if status != "approved":
        return [
            Issue(
                "reference_not_approved",
                f"{what} is {status}; approve it first",
                path=path,
                detail={"id": str(version_id)},
            )
        ]
    return []
