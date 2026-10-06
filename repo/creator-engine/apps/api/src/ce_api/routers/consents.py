"""Consents (§30, §32; Phase 10: the data model and endpoints; the flows stay off until V1).

A consent records that a real person allowed their face and/or voice to be used: a spoken phrase and
statement captured on video or audio, what it permits, where, and until when. Digital twins and
voice cloning need a valid (verified, unexpired, unrevoked) consent and are disabled by the platform
flag `digital_twins_enabled=false` until V1, so `:start` and `:submit` refuse while it is off;
listing, reading and revoking always work. Verification (face and voice match against the consent
media, the phrase heard by ASR) and the envelope encryption of consent media are V1 work: a submitted
consent stays unverified here, and nothing treats an unverified consent as valid.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

import sqlalchemy as sa
from ce_core.errors import ConflictError, InvalidInputError, Issue, PolicyDeniedError
from ce_db.models.research import Consent
from fastapi import APIRouter, Request
from pydantic import Field

from ce_api.common import audit
from ce_api.deps import DbSession, Reader, ServicesDep, Writer
from ce_api.references import asset_issues
from ce_api.schemas import Body, Out, examples
from ce_api.versioning import get_scoped, lock_scoped

router = APIRouter(tags=["consents"])

PHRASE_WORDS = (
    "amber", "river", "seven", "lantern", "maple", "orbit", "velvet", "harbor", "cobalt", "meadow",
    "pixel", "summit", "willow", "canyon", "ember", "glacier", "nectar", "quartz", "saffron", "tundra",
)  # fmt: skip
STATEMENT = (
    "I, {name}, agree that {org} may use my {scope} to create AI-generated media for the uses listed in "
    "this consent, until {until} or until I revoke it. My phrase is: {phrase}."
)


class ConsentStart(Body):
    model_config = examples(
        [
            {
                "subject_name": "Sam Doe",
                "scope": "voice",
                "permitted_uses": {"voice_clone": True},
                "jurisdictions": ["EU"],
            }
        ]
    )
    subject_name: Annotated[str, Field(min_length=1, max_length=200)]
    scope: Literal["face", "voice", "both"]
    permitted_uses: dict[str, Any] = Field(default_factory=dict)
    jurisdictions: list[str] = Field(default_factory=list)
    valid_days: int = Field(default=365, ge=1, le=3650)


class ConsentSubmit(Body):
    model_config = examples([{"consent_media_asset_id": "0192f0a0-0000-7000-8000-000000000001"}])
    consent_media_asset_id: UUID


class ConsentOut(Out):
    id: UUID
    subject_name: str
    scope: str
    consent_media_asset_id: UUID | None
    phrase: str | None
    statement_text: str
    permitted_uses: dict[str, Any]
    jurisdictions: list[str]
    face_match_score: float | None
    voice_match_score: float | None
    verified_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime
    status: str = Field(default="", description="pending_media | submitted_unverified | verified | revoked | expired")
    valid: bool = Field(default=False, description="verified, unexpired and unrevoked")


class ConsentStarted(Out):
    consent_id: UUID
    phrase: str
    statement_text: str
    upload_urls: dict[str, str] = Field(
        description="how to upload the consent recording: initiate an asset upload (kind video or audio), then submit"
    )


def _status(row: Consent, now: datetime) -> tuple[str, bool]:
    if row.revoked_at is not None:
        return "revoked", False
    if row.expires_at is not None and row.expires_at <= now:
        return "expired", False
    if row.verified_at is not None:
        return "verified", True
    return ("submitted_unverified" if row.consent_media_asset_id else "pending_media"), False


def _out(row: Consent, now: datetime) -> ConsentOut:
    status, valid = _status(row, now)
    return ConsentOut.model_validate(row).model_copy(update={"status": status, "valid": valid})


def _require_flows(services: Any) -> None:
    if not services.effective.bundle.app.features.digital_twins_enabled:
        raise PolicyDeniedError(
            "consent flows (digital twins, voice cloning) are disabled until V1 (digital_twins_enabled=false)",
            issues=[Issue("feature_flag", "digital_twins_enabled=false")],
        )


@router.post("/v1/consents:start", response_model=ConsentStarted, status_code=201)
async def start_consent(
    body: ConsentStart, principal: Writer, request: Request, session: DbSession, services: ServicesDep
) -> ConsentStarted:
    _require_flows(services)
    now = services.clock()
    phrase = " ".join(secrets.choice(PHRASE_WORDS) for _ in range(4))
    until = now + timedelta(days=body.valid_days)
    statement = STATEMENT.format(
        name=body.subject_name, org="this organization", scope={"both": "face and voice"}.get(body.scope, body.scope),
        until=until.date().isoformat(), phrase=phrase,
    )  # fmt: skip
    row = Consent(
        org_id=principal.org_id, subject_name=body.subject_name, scope=body.scope, phrase=phrase,
        statement_text=statement, permitted_uses=body.permitted_uses, jurisdictions=body.jurisdictions,
        expires_at=until,
    )  # fmt: skip
    session.add(row)
    await session.flush()
    await audit(session, principal, "consent.start", "consent", row.id, request=request,
                after={"scope": body.scope, "subject_name": body.subject_name})  # fmt: skip
    return ConsentStarted(
        consent_id=row.id, phrase=phrase, statement_text=statement,
        upload_urls={"initiate": "/v1/assets:initiate-upload", "submit": f"/v1/consents/{row.id}:submit"},
    )  # fmt: skip


@router.post("/v1/consents/{consent_id}:submit", response_model=ConsentOut)
async def submit_consent(
    consent_id: UUID, body: ConsentSubmit, principal: Writer, request: Request, session: DbSession,
    services: ServicesDep,
) -> ConsentOut:  # fmt: skip
    row = await lock_scoped(session, Consent, principal.ctx, consent_id, "consent")  # 404 first (I12)
    _require_flows(services)
    if row.revoked_at is not None:
        raise ConflictError("the consent was revoked", issues=[Issue("consent", "revoked")])
    issues = await asset_issues(
        session, principal.org_id, [body.consent_media_asset_id], path="/consent_media_asset_id"
    )
    if issues:
        raise InvalidInputError("invalid consent recording", issues=issues)
    row.consent_media_asset_id = body.consent_media_asset_id
    await session.flush()
    await audit(session, principal, "consent.submit", "consent", row.id, request=request,
                after={"consent_media_asset_id": str(body.consent_media_asset_id), "verification": "V1"})  # fmt: skip
    await session.refresh(row)
    return _out(row, services.clock())


@router.post("/v1/consents/{consent_id}:revoke", response_model=ConsentOut)
async def revoke_consent(
    consent_id: UUID, principal: Writer, request: Request, session: DbSession, services: ServicesDep
) -> ConsentOut:
    row = await lock_scoped(session, Consent, principal.ctx, consent_id, "consent")
    if row.revoked_at is None:
        row.revoked_at = services.clock()
        await session.flush()
        await audit(session, principal, "consent.revoke", "consent", row.id, request=request)
        await session.refresh(row)
    return _out(row, services.clock())


@router.get("/v1/consents", response_model=list[ConsentOut])
async def list_consents(principal: Reader, session: DbSession, services: ServicesDep) -> list[ConsentOut]:
    rows = (
        await session.execute(
            sa.select(Consent).where(Consent.org_id == principal.org_id).order_by(Consent.created_at.desc()).limit(500)
        )
    ).scalars()
    now = services.clock()
    return [_out(r, now) for r in rows]


@router.get("/v1/consents/{consent_id}", response_model=ConsentOut)
async def get_consent(consent_id: UUID, principal: Reader, session: DbSession, services: ServicesDep) -> ConsentOut:
    return _out(await get_scoped(session, Consent, principal.ctx, consent_id, "consent"), services.clock())
