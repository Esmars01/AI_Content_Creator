"""Mock QC metrics (§26): seeded scores; `MOCK_QC_FAIL_RATE` sets the share that fail.

Failure injection for the QC gate tests: `MOCK_QC_FAIL_NODES` lists `node_key@n` entries
(comma-separated) — the gating metrics of that `qc.shot` node fail on its first `n` attempts
(`labels.qc_attempt` < n), whatever the media.
"""

from __future__ import annotations

import os

from ce_contracts.common import RunContext
from ce_contracts.interfaces import QCMetric
from ce_contracts.models import QCMetricRequest, QCMetricResult

from ce_plugins_mock._base import MockAdapter, env_float
from ce_plugins_mock._media import seed_int

__all__ = ["MockQC"]

# (metric name, pass score, fail score, advisory)
_METRICS = {
    "qc.lipsync": ("lipsync_score", 6.5, 2.0, True),  # advisory until the SyncNet weights license is verified (§26)
    "qc.vqa": ("visual_quality", 0.8, 0.3, False),
    "qc.speech_quality": ("speech_quality", 3.8, 2.2, False),
}


def _injected(labels: dict[str, str]) -> bool:
    node, attempt = labels.get("node"), int(labels.get("qc_attempt", "0") or 0)
    for entry in os.environ.get("MOCK_QC_FAIL_NODES", "").split(","):
        key, _, count = entry.strip().partition("@")
        if key and key == node and attempt < int(count or 1):
            return True
    return False


class MockQC(MockAdapter, QCMetric):
    def _measure(self, capability: str, request: QCMetricRequest, seed: int) -> QCMetricResult:
        name, good, bad, advisory = _METRICS[capability]
        rate = float(request.labels.get("fail_rate", env_float("MOCK_QC_FAIL_RATE", 0.0)))
        draw = seed_int(capability, request.media.sha256, seed) % 10_000 / 10_000.0
        failed = draw < rate or (not advisory and _injected(dict(request.labels)))
        score = bad if failed else good
        return QCMetricResult(
            metric=name, score=score, metrics={name: score, "mock": 1.0}, passed=not failed, advisory=advisory
        )

    async def run_qc_lipsync(self, request: QCMetricRequest, ctx: RunContext) -> QCMetricResult:
        return self._measure("qc.lipsync", request, ctx.seed)

    async def run_qc_vqa(self, request: QCMetricRequest, ctx: RunContext) -> QCMetricResult:
        return self._measure("qc.vqa", request, ctx.seed)

    async def run_qc_speech_quality(self, request: QCMetricRequest, ctx: RunContext) -> QCMetricResult:
        return self._measure("qc.speech_quality", request, ctx.seed)
