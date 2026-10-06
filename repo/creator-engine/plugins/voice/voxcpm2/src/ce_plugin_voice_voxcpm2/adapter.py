"""VoxCPM2 adapter (§21). `voice.prepare` bundles the trimmed reference clip and its transcript
(VoxCPM2 builds its prompt cache from the audio at generation time; there is no tensor to cache
across processes in `voxcpm` 2.0.3). Synthesis:

- with a prepared voice and an instruction: `reference_wav_path` + `"(instruction)text"`
  (controllable cloning);
- with a prepared voice, a transcript and no instruction: `prompt_wav_path` + `prompt_text` +
  `reference_wav_path` (the README's "ultimate cloning");
- without a prepared voice: `"(voice description, instruction)text"` (voice design) with a fixed
  seed, flagged `voice_source: description` — a previz voice, not a final one.

VoxCPM2 reads the language from the text (no language argument); requests outside its 30
languages are refused."""

from __future__ import annotations

from typing import Any

from ce_contracts import models as m
from ce_contracts.common import LoadContext
from ce_plugin_kit.speech import SpeechAdapter

from ce_plugin_voice_voxcpm2.translator import VoxCPM2Translator

__all__ = ["LANGUAGES", "VoxCPM2Adapter", "check_language"]

# README "Supported Languages (30)" as ISO 639-1 codes (Tagalog `tl`, Norwegian `no`/`nb`).
LANGUAGES = frozenset(
    {
        "ar", "my", "zh", "da", "nl", "en", "fi", "fr", "de", "el", "he", "hi", "id", "it", "ja", "km",
        "ko", "lo", "ms", "no", "nb", "pl", "pt", "ru", "es", "sw", "sv", "tl", "th", "tr", "vi",
    }
)  # fmt: skip


def check_language(code: str) -> str:
    primary = code.split("-")[0].lower()
    if primary not in LANGUAGES:
        raise ValueError(f"VoxCPM2 does not speak {code!r}")
    return primary


class VoxCPM2Adapter(SpeechAdapter):
    native_rate = 48_000
    translator = VoxCPM2Translator()

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_voice_voxcpm2.backend import VoxCPM2Backend

        assert self.paths is not None
        return VoxCPM2Backend(model_dir=self.paths.model("voxcpm2"), defaults=self.defaults)

    def synth_options(
        self, request: m.TTSRequest, engine: dict[str, Any], voice: dict[str, Any], seed: int
    ) -> dict[str, Any]:
        check_language(request.language)
        files, meta = voice["files"], voice["meta"]
        instruction = str(engine.get("instruction") or "")
        reference = str(files["reference.wav"]) if "reference.wav" in files else None
        description = None
        if reference is None:
            description = request.voice.description or str(self.defaults.get("default_voice", ""))
            seed = int(self.defaults.get("description_seed", 7))
        return {
            "seed": seed,
            "instruction": instruction,
            "reference": reference,
            "transcript": meta.get("transcript") if reference and not instruction else None,
            "description": description,
            "cfg_value": float(self.defaults.get("cfg_value", 2.0)),
            "inference_timesteps": int(self.defaults.get("inference_timesteps", 10)),
        }
