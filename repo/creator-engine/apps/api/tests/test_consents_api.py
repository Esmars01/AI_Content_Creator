"""Consents (§30, §32; Phase 10): the endpoints exist, the flows are off until V1. A consent is
never valid without verification, and revoking always works."""

from __future__ import annotations

from typing import Any

import pytest
from ce_api.testing import ApiHarness, ApiTenant, upload_asset
from ce_testing.placeholders import placeholder_png

pytestmark = pytest.mark.infra


def _flows(harness: ApiHarness, enabled: bool) -> None:
    effective: Any = harness.services.effective
    app = effective.bundle.app
    features = app.features.model_copy(update={"digital_twins_enabled": enabled})
    object.__setattr__(effective.bundle, "app", app.model_copy(update={"features": features}))


async def test_consent_flows_are_off_until_v1(harness: ApiHarness, owner: ApiTenant) -> None:
    refused = await owner.client.post("/v1/consents:start", json={"subject_name": "Sam", "scope": "voice"})
    assert refused.status_code == 403 and refused.json()["code"] == "policy_denied"
    assert (await owner.client.get("/v1/consents")).json() == []


async def test_a_recorded_consent_stays_unverified_and_can_be_revoked(harness: ApiHarness, owner: ApiTenant) -> None:
    media = await upload_asset(harness, owner, placeholder_png("consent"), mime="image/png", kind="image")
    _flows(harness, True)
    try:
        started = await owner.client.post(
            "/v1/consents:start", json={"subject_name": "Sam Doe", "scope": "both", "jurisdictions": ["EU"]}
        )
        assert started.status_code == 201, started.text
        body = started.json()
        assert len(body["phrase"].split()) == 4 and body["phrase"] in body["statement_text"]
        assert "face and voice" in body["statement_text"] and body["upload_urls"]["initiate"]
        consent_id = body["consent_id"]
        assert (await owner.client.get(f"/v1/consents/{consent_id}")).json()["status"] == "pending_media"
        submitted = await owner.client.post(
            f"/v1/consents/{consent_id}:submit", json={"consent_media_asset_id": media["id"]}
        )
        assert submitted.status_code == 200
        assert submitted.json()["status"] == "submitted_unverified" and submitted.json()["valid"] is False
    finally:
        _flows(harness, False)
    revoked = await owner.client.post(f"/v1/consents/{consent_id}:revoke")
    assert revoked.json()["status"] == "revoked" and revoked.json()["valid"] is False
    viewer = await harness.add_member(owner.org_id, "viewer")
    assert (await viewer.client.post(f"/v1/consents/{consent_id}:revoke")).status_code == 403
