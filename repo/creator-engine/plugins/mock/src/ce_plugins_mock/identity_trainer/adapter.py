"""Mock IdentityTrainer (V1 LoRA training, §39.5): a labelled placeholder, never weights."""

from __future__ import annotations

import json

from ce_contracts.common import RunContext
from ce_contracts.interfaces import IdentityTrainer
from ce_contracts.models import IdentityTrainRequest, IdentityTrainResult

from ce_plugins_mock._base import MockAdapter

__all__ = ["MockIdentityTrainer"]


class MockIdentityTrainer(MockAdapter, IdentityTrainer):
    async def run_identity_train(self, request: IdentityTrainRequest, ctx: RunContext) -> IdentityTrainResult:
        out = self.workdir(ctx, "train") / "lora.json"
        out.write_text(
            json.dumps(
                {
                    "mock": True,
                    "note": "placeholder: identity training is a V1 capability",
                    "appearance_version_id": request.appearance_version_id,
                    "dataset": sorted(r.sha256 for r in request.dataset),
                    "base_model": request.base_model,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        ref = await self.write(ctx, out, "other", role="lora", mime="application/json")
        return IdentityTrainResult(lora=ref, steps=0)
