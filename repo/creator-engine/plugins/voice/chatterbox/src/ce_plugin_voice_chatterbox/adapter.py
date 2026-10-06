"""Chatterbox adapters (§21): `voice.tts` and `voice.clone_prepare` on the shared `SpeechAdapter`
(pauses as inserted silence, rate as a ±10% time-stretch, conditioning bundles). `voice.prepare`
stores the trimmed reference and the engine's `Conditionals` (`conds.pt`), so every segment of a
voice reuses the same speaker embedding; without a prepared voice the checkpoint's built-in voice is
used."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_plugin_kit.speech import SpeechAdapter

from ce_plugin_voice_chatterbox.translator import (
    ChatterboxMultilingualTranslator,
    ChatterboxTranslator,
    ChatterboxTurboTranslator,
)

__all__ = ["ChatterboxAdapter", "ChatterboxMultilingualAdapter", "ChatterboxTurboAdapter"]


class ChatterboxAdapter(SpeechAdapter):
    native_rate = 24_000  # S3GEN_SR
    variant = "base"
    translator = ChatterboxTranslator()

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_voice_chatterbox.backend import ChatterboxBackend

        assert self.paths is not None
        return ChatterboxBackend(self.paths.model(), variant=self.variant, defaults=self.defaults)

    def synth_options(
        self, request: m.TTSRequest, engine: dict[str, Any], voice: dict[str, Any], seed: int
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "seed": seed,
            "conds": str(voice["files"]["conds.pt"]) if "conds.pt" in voice["files"] else None,
            "temperature": float(self.defaults.get("temperature", 0.8)),
        }
        if "exaggeration" in engine:
            options["exaggeration"] = float(engine["exaggeration"])
            options["cfg_weight"] = float(engine["cfg_weight"])
        return options


class ChatterboxTurboAdapter(ChatterboxAdapter):
    variant = "turbo"
    translator = ChatterboxTurboTranslator()


class ChatterboxMultilingualAdapter(ChatterboxAdapter):
    variant = "multilingual"
    translator = ChatterboxMultilingualTranslator()

    def synth_options(
        self, request: m.TTSRequest, engine: dict[str, Any], voice: dict[str, Any], seed: int
    ) -> dict[str, Any]:
        options = super().synth_options(request, engine, voice, seed)
        options["language_id"] = request.language.split("-")[0].lower()
        return options
