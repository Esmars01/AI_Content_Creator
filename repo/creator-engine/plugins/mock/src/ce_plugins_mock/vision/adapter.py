"""Mock VLM and OCR: seeded answers with low confidence; OCR finds no text.

A request whose JSON schema asks for `apparent_age` (the identity pack's age check) gets a seeded
adult age between 25 and 45 — a mock answer, flagged `mock`, never evidence about a real face.
A request whose JSON schema asks for `events` (the screen-analysis summary, §27) gets one labelled
mock event per requested window (the whole clip without one) — the shape a real VLM returns, with
nothing a model would have seen. A `defects` schema (the VLM judge, §26) gets no defects, unless
`MOCK_VLM_DEFECTS` injects them for tests: comma-separated `check:severity[@node_key]` entries,
answered with confidence 0.9. A critique schema (`problems`) gets seeded scores and no problems."""

from __future__ import annotations

import os

from ce_contracts.common import RunContext
from ce_contracts.interfaces import VideoAnalyzer, VisionAnalyzer
from ce_contracts.models import OcrRequest, OcrResult, VisionRequest, VisionResult

from ce_plugins_mock._base import MockAdapter
from ce_plugins_mock._media import seed_int

__all__ = ["MockVision"]


class MockVision(MockAdapter, VisionAnalyzer, VideoAnalyzer):
    def _answer(self, request: VisionRequest) -> VisionResult:
        value = seed_int(request.media.sha256, request.question) % 1000 / 1000.0
        answer: dict[str, object] = {"mock": True, "yes": value >= 0.5, "score": value}
        properties = dict(request.json_schema.get("properties", {}))
        if "apparent_age" in properties:  # the identity pack's age check (§17.3): a seeded adult value
            answer["apparent_age"] = round(25.0 + value * 20.0, 1)
        if "defects" in properties:  # the VLM judge (§26)
            node = request.labels.get("node")
            defects = []
            for entry in os.environ.get("MOCK_VLM_DEFECTS", "").split(","):
                spec, _, only = entry.strip().partition("@")
                check, _, severity = spec.partition(":")
                if check and (not only or only == node):
                    defects.append(
                        {"check": check, "severity": severity or "critical", "t_s": 0.0, "description": "mock defect"}
                    )
            answer["defects"] = defects
            if defects:
                return VisionResult(answer=answer, confidence=0.9)
        if "elements" in properties:  # world element checks (§19.6): every listed element seen, mock
            items = dict(dict(properties["elements"]).get("items") or {}).get("properties") or {}
            keys = list(dict(items.get("key") or {}).get("enum") or [])
            answer["elements"] = [{"key": k, "present": True, "state_ok": None} for k in keys]
            answer["relations"] = []
        if "problems" in properties:  # the Creative Director's pass over the proxy (§26)
            answer["scores"] = {"realism": round(0.5 + value * 0.4, 3), "visual_quality": round(0.5 + value * 0.4, 3)}
            answer["problems"] = []
        if "events" in properties:
            start = request.window_s[0] if request.window_s else 0.0
            answer["events"] = [{"t_s": round(start, 3), "description": "mock: on-screen activity (no model ran)"}]
            answer["summary"] = "mock summary: a VLM has not looked at this recording"
        return VisionResult(answer=answer, confidence=0.2)

    async def run_vision_image(self, request: VisionRequest, ctx: RunContext) -> VisionResult:
        return self._answer(request)

    async def run_vision_video(self, request: VisionRequest, ctx: RunContext) -> VisionResult:
        return self._answer(request)

    async def run_vision_ocr(self, request: OcrRequest, ctx: RunContext) -> OcrResult:
        return OcrResult(frames=[])
