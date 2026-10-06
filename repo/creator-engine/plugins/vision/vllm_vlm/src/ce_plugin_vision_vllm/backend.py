"""The real vLLM backend (vLLM 0.30.0 offline `LLM.chat` with `StructuredOutputsParams(json=...)`):
imported only on a `vllm` worker. Media are passed as `file://` URLs under the worker's scratch
directory (`allowed_local_media_path`). **Untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["VLLMBackend"]


class VLLMBackend:
    def __init__(self, *, model_dir: Path, media_root: str, defaults: dict[str, Any]) -> None:
        from vllm import LLM

        self.llm = LLM(
            model=str(model_dir),
            max_model_len=int(defaults.get("max_model_len", 32768)),
            gpu_memory_utilization=float(defaults.get("gpu_memory_utilization", 0.85)),
            limit_mm_per_prompt={"image": 1, "video": 1},
            allowed_local_media_path=str(Path(media_root).resolve()),
            seed=0,
            trust_remote_code=False,
        )
        self.max_tokens = int(defaults.get("max_tokens", 2048))
        self.temperature = float(defaults.get("temperature", 0.0))

    def ask(self, kind: str, media: str, prompt: str, schema: dict[str, Any], seed: int) -> str:
        from vllm import SamplingParams
        from vllm.sampling_params import StructuredOutputsParams

        part = {"type": f"{kind}_url", f"{kind}_url": {"url": Path(media).resolve().as_uri()}}
        messages = [{"role": "user", "content": [part, {"type": "text", "text": prompt}]}]
        params = SamplingParams(
            temperature=self.temperature, max_tokens=self.max_tokens, seed=seed,
            structured_outputs=StructuredOutputsParams(json=schema),
        )  # fmt: skip
        (output,) = self.llm.chat(messages, params, chat_template_kwargs={"enable_thinking": False})
        return str(output.outputs[0].text)

    def close(self) -> None:
        self.llm = None
