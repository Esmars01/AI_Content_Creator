"""Uploaded files as identity, world and voice references (cutover §4).

A user can bring their own media instead of generated candidates. The policy (§17.3, §21) is the same
as for generated media, plus one rule:

- **A real, identifiable person** (a photo of someone's face, a recording of someone's voice) is a
  digital twin or a cloned voice. Both need a verified consent, a V1 feature that is switched off
  (`identity.digital_twins_enabled: false`). So an uploaded face or voice is accepted only with the
  uploader's explicit attestation that it is *not* a real person (a synthetic or licensed character
  design, a synthetic voice), recorded on the version and in the audit log. A plate needs the
  attestation that it shows no identifiable people and that the uploader holds the rights.
- The upload must be usable: a face's shorter side, a plate's size, a voice reference's duration and
  sample rate (`uploads.references`), read from the validation probe.

Generated candidates (`rights.source == "generated"`) and labeled seed placeholders keep their own
rules and need neither.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from ce_config.schemas import ReferenceUploadLimits
from ce_core.errors import InvalidInputError, Issue
from ce_db.models.assets import Asset
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "FACE_ATTESTATION",
    "PLATE_ATTESTATION",
    "VOICE_ATTESTATION",
    "FaceAttestation",
    "PlateAttestation",
    "VoiceAttestation",
    "attestation_missing",
    "attested",
    "check_uploaded_reference",
    "is_uploaded",
    "media_size",
]

FACE_ATTESTATION = "not_a_real_person"
PLATE_ATTESTATION = "no_identifiable_people_rights_held"
VOICE_ATTESTATION = "synthetic_voice_not_a_person"
FaceAttestation = Literal["not_a_real_person"]
PlateAttestation = Literal["no_identifiable_people_rights_held"]
VoiceAttestation = Literal["synthetic_voice_not_a_person"]

_REAL_PERSON = (
    "a real, identifiable person needs the digital-twin consent path (a verified consent), which is not "
    "available yet (V1)"
)


def is_uploaded(asset: Asset) -> bool:
    """A user's own file: neither a generated candidate nor a labeled seed placeholder."""
    rights = asset.rights or {}
    return rights.get("source") != "generated" and not rights.get("placeholder")


def media_size(asset: Asset) -> tuple[int, int, float, int]:
    """(width, height, duration seconds, sample rate) from the validation probe; 0 when unknown."""
    probe = asset.probe or {}
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    duration = 0.0
    rate = 0
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") == "video":
            width = width or int(stream.get("width") or 0)
            height = height or int(stream.get("height") or 0)
        if stream.get("codec_type") == "audio":
            rate = rate or int(float(stream.get("sample_rate") or 0))
            duration = duration or float(stream.get("duration") or 0.0)
    duration = duration or float((probe.get("format") or {}).get("duration") or 0.0)
    return width, height, duration, rate


Use = Literal["face", "plate", "voice"]
_EXPECTED: dict[str, str] = {"face": FACE_ATTESTATION, "plate": PLATE_ATTESTATION, "voice": VOICE_ATTESTATION}


def attestation_missing(asset: Asset | None, use: Use) -> bool:
    """For approvals: an uploaded reference without the attestation recorded on the asset."""
    if asset is None or not is_uploaded(asset):
        return False
    record = ((asset.rights or {}).get("attestations") or {}).get(use) or {}
    return record.get("attestation") != _EXPECTED[use]


async def check_uploaded_reference(
    session: AsyncSession,
    org_id: UUID,
    asset_id: UUID,
    *,
    use: Use,
    attestation: str | None,
    limits: ReferenceUploadLimits,
    user_id: UUID,
    at: datetime,
    path: str = "/asset_id",
) -> dict[str, Any] | None:
    """Raises when an uploaded file cannot be this reference. Otherwise records the attestation on
    the asset (`rights.attestations.<use>`, who and when) and returns that record; None for a
    generated or placeholder asset, which needs nothing. The caller has already checked that the
    asset exists, is ready and has the right media family."""
    asset = await session.get(Asset, asset_id)
    if asset is None or asset.org_id != org_id or not is_uploaded(asset):
        return None
    issues: list[Issue] = []
    width, height, duration, rate = media_size(asset)
    expected = _EXPECTED[use]
    if attestation != expected:
        message = {
            "face": f"an uploaded face needs the attestation {FACE_ATTESTATION!r}: {_REAL_PERSON}",
            "plate": f"an uploaded plate needs the attestation {PLATE_ATTESTATION!r} (no identifiable people; "
            "you hold the rights)",
            "voice": f"an uploaded voice reference needs the attestation {VOICE_ATTESTATION!r}: {_REAL_PERSON}",
        }[use]
        issues.append(Issue("upload_attestation", message, path="/attestation"))
    if use == "face" and min(width, height) < limits.face_min_px:
        issues.append(
            Issue(
                "upload_too_small",
                f"a face reference needs at least {limits.face_min_px} px on its shorter side",
                path=path,
                detail={"width": width, "height": height},
            )
        )
    if use == "plate" and (width < limits.plate_min_width_px or height < limits.plate_min_height_px):
        issues.append(
            Issue(
                "upload_too_small",
                f"a plate needs at least {limits.plate_min_width_px}×{limits.plate_min_height_px} px",
                path=path,
                detail={"width": width, "height": height},
            )
        )
    if use == "voice":
        if not (limits.voice_min_s <= duration <= limits.voice_max_s):
            issues.append(
                Issue(
                    "upload_duration",
                    f"a voice reference lasts {limits.voice_min_s:g}-{limits.voice_max_s:g} s of clean speech",
                    path=path,
                    detail={"duration_s": round(duration, 2)},
                )
            )
        if rate < limits.voice_min_sample_rate:
            issues.append(
                Issue(
                    "upload_sample_rate",
                    f"a voice reference needs at least {limits.voice_min_sample_rate} Hz",
                    path=path,
                    detail={"sample_rate": rate},
                )
            )
    if issues:
        raise InvalidInputError(f"the uploaded file cannot be used as a {use} reference", issues=issues)
    record = attested({"source": "upload", "asset_id": str(asset_id), "attestation": expected}, user_id, at)
    rights = dict(asset.rights or {})
    rights["attestations"] = {**dict(rights.get("attestations") or {}), use: record}
    asset.rights = rights
    await session.flush()
    return record


def attested(record: dict[str, Any], user_id: UUID, at: datetime) -> dict[str, Any]:
    return {**record, "attested_by": str(user_id), "attested_at": at.isoformat()}
