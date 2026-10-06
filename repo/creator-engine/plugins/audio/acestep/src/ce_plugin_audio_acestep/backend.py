"""The real ACE-Step 1.5 backend (`acestep.handler.AceStepHandler`, `acestep.inference.generate_music`,
as in `docs/en/INFERENCE.md` at commit ca1e85f): imported only on an `audio` worker whose image has
the ACE-Step repository at that commit on `PYTHONPATH`.

ACE-Step may rewrite model-side code files in its checkpoint directory when they differ from the
installed code (`_sync_model_code_if_needed`); the model cache is immutable, so the backend builds
a private checkpoint directory per process: small text files copied, weights symlinked.

Status: implemented against the upstream code, **untested on a GPU** (rule 5)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

__all__ = ["AceStepBackend", "private_checkpoints"]

COPY_SUFFIXES = {".py", ".json", ".jinja", ".txt"}


def private_checkpoints(cached: Path, dest: Path) -> Path:
    """A writable view of the cached snapshot: text files copied, everything else symlinked."""
    for src in Path(cached).rglob("*"):
        if src.is_dir():
            continue
        target = dest / src.relative_to(cached)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            continue
        if src.suffix in COPY_SUFFIXES:
            shutil.copy2(src, target)
        else:
            target.symlink_to(src.resolve())
    return dest


class AceStepBackend:
    def __init__(self, *, checkpoints: Path, scratch: str, defaults: dict[str, Any]) -> None:
        root = private_checkpoints(Path(checkpoints), Path(scratch) / "acestep_checkpoints")
        os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(root)
        import torch
        from acestep.handler import AceStepHandler
        from acestep.llm_inference import LLMHandler

        self.torch = torch
        self.dit = AceStepHandler()
        status, ok = self.dit.initialize_service(
            project_root="",
            config_path=str(defaults.get("dit", "acestep-v15-turbo")),
            device="cuda",
            offload_to_cpu=bool(defaults.get("offload_to_cpu", False)),
        )
        if not ok:
            raise RuntimeError(f"ACE-Step failed to initialize: {status}")
        self.llm = None
        if defaults.get("use_lm", True):
            self.llm = LLMHandler()
            self.llm.initialize(
                checkpoint_dir=str(root), lm_model_path=str(defaults.get("lm", "acestep-5Hz-lm-1.7B")),
                backend=str(defaults.get("lm_backend", "pt")), device="cuda",
            )  # fmt: skip

    def generate(self, options: dict[str, Any], workdir: str) -> str:
        from acestep.inference import GenerationConfig, GenerationParams, generate_music

        self.torch.manual_seed(int(options["seed"]))
        params = GenerationParams(
            caption=str(options["caption"]), lyrics="[Instrumental]", instrumental=True,
            bpm=options.get("bpm"), duration=float(options["duration_s"]),
            inference_steps=int(options["steps"]), shift=float(options["shift"]), seed=int(options["seed"]),
            thinking=self.llm is not None,
        )  # fmt: skip
        config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[int(options["seed"])], audio_format="wav")
        result = generate_music(self.dit, self.llm, params, config, save_dir=workdir)
        if not result.success or not result.audios:
            raise RuntimeError(f"ACE-Step generation failed: {result.error or result.status_message}")
        return str(result.audios[0]["path"])

    def close(self) -> None:
        self.dit = self.llm = None  # type: ignore[assignment]
        self.torch.cuda.empty_cache()
