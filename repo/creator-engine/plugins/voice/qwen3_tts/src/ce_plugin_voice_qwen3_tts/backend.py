"""The real Qwen3-TTS backend (`qwen-tts` 0.1.1, `Qwen3TTSModel`, as in the Qwen3-TTS README):
imported only on a `tts` worker. **Untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["Qwen3TTSBackend"]


class Qwen3TTSBackend:
    def __init__(self, *, base_dir: Path, design_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        from qwen_tts import Qwen3TTSModel

        self.torch = torch
        self._cls = Qwen3TTSModel
        self._kwargs = {
            "device_map": "cuda:0",
            "dtype": torch.bfloat16,
            "attn_implementation": str(defaults.get("attn_implementation", "flash_attention_2")),
        }
        self.base = Qwen3TTSModel.from_pretrained(str(base_dir), **self._kwargs)
        self.design_dir = design_dir
        self._design: Any = None
        self._prompts: dict[str, Any] = {}

    def _designer(self) -> Any:
        if self._design is None:  # loaded on first use: most workers only clone
            self._design = self._cls.from_pretrained(str(self.design_dir), **self._kwargs)
        return self._design

    def _prompt(self, path: str) -> Any:
        if path not in self._prompts:
            from qwen_tts.inference.qwen3_tts_model import VoiceClonePromptItem

            data = self.torch.load(path, map_location="cuda:0", weights_only=True)
            self._prompts[path] = [VoiceClonePromptItem(**data)]
        return self._prompts[path]

    def synthesize(self, text: str, options: dict[str, Any]) -> Any:
        self.torch.manual_seed(int(options["seed"]))
        if options.get("prompt"):
            wavs, _ = self.base.generate_voice_clone(
                text=text, language=options["language"], voice_clone_prompt=self._prompt(options["prompt"])
            )
        else:
            self.torch.manual_seed(int(options["description_seed"]))
            wavs, _ = self._designer().generate_voice_design(
                text=text, language=options["language"], instruct=str(options.get("description") or "")
            )
        return wavs[0]

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        (item,) = self.base.create_voice_clone_prompt(
            ref_audio=str(reference_wav), ref_text=transcript or None, x_vector_only_mode=not transcript
        )
        out = workdir / "prompt.pt"
        self.torch.save(
            {
                "ref_code": item.ref_code,
                "ref_spk_embedding": item.ref_spk_embedding,
                "x_vector_only_mode": item.x_vector_only_mode,
                "icl_mode": item.icl_mode,
                "ref_text": item.ref_text,
            },
            out,
        )
        return {"prompt.pt": out}

    def design(self, description: str, sample_text: str, language: str, seed: int) -> Any:
        from ce_plugin_voice_qwen3_tts.adapter import language_name

        self.torch.manual_seed(seed)
        wavs, _ = self._designer().generate_voice_design(
            text=sample_text, language=language_name(language), instruct=description
        )
        return wavs[0]

    def close(self) -> None:
        self.base = None
        self._design = None
        self.torch.cuda.empty_cache()
