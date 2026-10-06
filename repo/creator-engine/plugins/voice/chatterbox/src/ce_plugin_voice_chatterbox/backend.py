"""The real Chatterbox backends: imported only on a `tts` worker where the chatterbox repository is
installed at commit 5de7a54 (Multilingual V3 needs it; PyPI 0.1.7 only has V2). Implemented against
that code; **untested on a GPU** (rule 5).

Outputs carry Resemble's Perth implicit watermark (applied inside `generate()`); the final mix also
gets AudioSeal (§27), so provenance is layered, never removed."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["ChatterboxBackend"]


class ChatterboxBackend:
    def __init__(self, model_dir: Path, *, variant: str, defaults: dict[str, Any]) -> None:
        import torch

        self.torch = torch
        self.variant = variant
        if variant == "turbo":
            from chatterbox.tts_turbo import ChatterboxTurboTTS

            self.model = ChatterboxTurboTTS.from_local(str(model_dir), "cuda")
        elif variant == "multilingual":
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            self.model = ChatterboxMultilingualTTS.from_local(
                str(model_dir), "cuda", t3_model=str(defaults.get("t3_model", "v3"))
            )
        else:
            from chatterbox.tts import ChatterboxTTS

            self.model = ChatterboxTTS.from_local(str(model_dir), "cuda")
        self.default_conds = self.model.conds  # the checkpoint's built-in voice (conds.pt)
        self._conds_path: str | None = None

    def _use_voice(self, conds: str | None) -> None:
        if conds == self._conds_path:
            return
        if conds is None:
            self.model.conds = self.default_conds
        else:
            from chatterbox.tts import Conditionals

            self.model.conds = Conditionals.load(conds, map_location="cuda").to("cuda")
        self._conds_path = conds

    def synthesize(self, text: str, options: dict[str, Any]) -> Any:
        self.torch.manual_seed(int(options["seed"]))
        self._use_voice(options.get("conds"))
        kwargs: dict[str, Any] = {"temperature": float(options.get("temperature", 0.8))}
        if self.variant != "turbo" and "exaggeration" in options:
            kwargs["exaggeration"] = float(options["exaggeration"])
            kwargs["cfg_weight"] = float(options["cfg_weight"])
        if self.variant == "multilingual":
            kwargs["language_id"] = options["language_id"]
        wav = self.model.generate(text, **kwargs)
        return wav.squeeze(0).detach().cpu().numpy()

    def prepare(self, reference_wav: Path, transcript: str, workdir: Path) -> dict[str, Path]:
        self.model.prepare_conditionals(str(reference_wav))
        out = workdir / "conds.pt"
        self.model.conds.save(out)
        self._conds_path = None  # the next synthesize() reloads the requested voice
        return {"conds.pt": out}

    def close(self) -> None:
        self.model = None
        self.torch.cuda.empty_cache()
