"""Qwen3-TTS adapter (§21): cloning with the Base model from a prepared voice prompt
(`voice.prepare` stores the reference clip and the model's voice-clone prompt tensors), designed
voices with the VoiceDesign model. Without a prepared voice, segments are designed from the voice
description with a fixed seed and the artifact says so (`voice_source: description`): fine for a
previz, not for a final (timbre can drift between segments)."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_plugin_kit.speech import SpeechAdapter

from ce_plugin_voice_qwen3_tts.translator import Qwen3TTSTranslator

__all__ = ["LANGUAGES", "Qwen3TTSAdapter", "language_name"]

LANGUAGES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean", "de": "German",
    "fr": "French", "ru": "Russian", "pt": "Portuguese", "es": "Spanish", "it": "Italian",
}  # fmt: skip


def language_name(code: str) -> str:
    primary = code.split("-")[0].lower()
    if primary not in LANGUAGES:
        raise ValueError(f"Qwen3-TTS does not speak {code!r}")
    return LANGUAGES[primary]


class Qwen3TTSAdapter(SpeechAdapter):
    native_rate = 24_000
    translator = Qwen3TTSTranslator()

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_voice_qwen3_tts.backend import Qwen3TTSBackend

        assert self.paths is not None
        return Qwen3TTSBackend(
            base_dir=self.paths.model("qwen3-tts-base"),
            design_dir=self.paths.model("qwen3-tts-voicedesign"),
            defaults=self.defaults,
        )

    def synth_options(
        self, request: m.TTSRequest, engine: dict[str, Any], voice: dict[str, Any], seed: int
    ) -> dict[str, Any]:
        files = voice["files"]
        return {
            "seed": seed,
            "language": language_name(request.language),
            "prompt": str(files["prompt.pt"]) if "prompt.pt" in files else None,
            "description": request.voice.description or str(self.defaults.get("default_voice", "")),
            "description_seed": int(self.defaults.get("description_seed", 7)),
        }
