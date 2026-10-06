"""The real SpeechBrain ECAPA-TDNN backend (`speechbrain.inference.speaker.EncoderClassifier`, as on
the model card), loaded from the local snapshot (no download). Imported only on an `asr` worker.
**Untested on a GPU** (rule 5); the model is small enough for CPU."""

from __future__ import annotations

from pathlib import Path

import numpy as np

__all__ = ["EcapaBackend"]


class EcapaBackend:
    def __init__(self, *, model_dir: Path, scratch: str) -> None:
        import torch
        from speechbrain.inference.speaker import EncoderClassifier

        self.torch = torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = EncoderClassifier.from_hparams(
            source=str(model_dir), savedir=str(Path(scratch) / "speechbrain_ecapa"), run_opts={"device": device}
        )

    def embed(self, samples16k: np.ndarray) -> np.ndarray:
        wav = self.torch.from_numpy(np.ascontiguousarray(samples16k, dtype=np.float32))[None, :]
        with self.torch.no_grad():
            emb = self.model.encode_batch(wav)
        return emb.reshape(-1).float().cpu().numpy()

    def close(self) -> None:
        self.model = None
