"""The real VoxCPM2 backend (`voxcpm` 2.0.3, `VoxCPM.generate`, as in the VoxCPM README): imported
only on a `tts` worker. **Untested on a GPU** (rule 5).

`voxcpm` 2.0.3 on PyPI has no `seed` argument (the GitHub `main` README shows one), so the backend
seeds torch's generators before each call."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["VoxCPM2Backend", "compose_text"]


def compose_text(text: str, *phrases: str | None) -> str:
    """`"(phrase, phrase)text"` — VoxCPM2's instruction / voice-design syntax — or the bare text."""
    head = ", ".join(p.strip() for p in phrases if p and p.strip())
    return f"({head}){text}" if head else text


class VoxCPM2Backend:
    def __init__(self, *, model_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        from voxcpm import VoxCPM

        self.torch = torch
        self.model = VoxCPM.from_pretrained(
            str(model_dir), load_denoiser=False, optimize=bool(defaults.get("torch_compile", False)), device="cuda:0"
        )
        self.sample_rate = int(self.model.tts_model.sample_rate)
        if self.sample_rate != 48_000:
            raise RuntimeError(f"VoxCPM2 returned {self.sample_rate} Hz; the adapter expects 48000")

    def _seed(self, seed: int) -> None:
        self.torch.manual_seed(seed)
        self.torch.cuda.manual_seed_all(seed)

    def synthesize(self, text: str, options: dict[str, Any]) -> Any:
        self._seed(int(options["seed"]))
        kwargs: dict[str, Any] = {
            "cfg_value": float(options["cfg_value"]),
            "inference_timesteps": int(options["inference_timesteps"]),
        }
        if options.get("reference"):
            kwargs["reference_wav_path"] = options["reference"]
            if options.get("transcript"):
                kwargs["prompt_wav_path"] = options["reference"]
                kwargs["prompt_text"] = options["transcript"]
            prompt = compose_text(text, options.get("instruction"))
        else:
            prompt = compose_text(text, options.get("description"), options.get("instruction"))
        return self.model.generate(text=prompt, **kwargs)

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        return {}  # the reference clip in the bundle is the whole conditioning

    def design(self, description: str, sample_text: str, language: str, seed: int) -> Any:
        self._seed(seed)
        return self.model.generate(text=compose_text(sample_text, description), cfg_value=2.0, inference_timesteps=10)

    def close(self) -> None:
        self.model = None
        self.torch.cuda.empty_cache()
