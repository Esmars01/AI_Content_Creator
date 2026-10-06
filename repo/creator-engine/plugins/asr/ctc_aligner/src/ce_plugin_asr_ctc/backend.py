"""The real Omnilingual ASR CTC 1B backend (`omnilingual-asr` 0.2.0 on fairseq2 0.6): imported
only on an `asr` worker. The model card is registered from the local files through a fairseq2
user asset card (`file://` checkpoint and tokenizer), so nothing is downloaded at load. Audio is
normalized as `omnilingual_asr.datasets.utils.audio.apply_audio_normalization` (layer norm over
the waveform) before the forward pass of `Wav2Vec2AsrModel`.

Status: implemented against the upstream code, **untested on a GPU** (rule 5); the separator
token (the character tokenizer's word boundary) and the card fields are [RV] until a smoke run."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["OmniCTCBackend"]

CARD = """name: ce_omniasr_tokenizer_v1
tokenizer_family: char_tokenizer
tokenizer: file://{tokenizer}

---

name: ce_omniasr_ctc_1b
model_family: wav2vec2_asr
model_arch: 1b
checkpoint: file://{checkpoint}
tokenizer_ref: ce_omniasr_tokenizer_v1
"""


class OmniCTCBackend:
    def __init__(self, *, model_dir: Path, defaults: dict[str, Any]) -> None:
        cards = Path(tempfile.mkdtemp(prefix="ce_fairseq2_cards_"))
        (cards / "ce_omniasr.yaml").write_text(
            CARD.format(
                tokenizer=Path(model_dir) / "omniASR_tokenizer.model", checkpoint=Path(model_dir) / "omniASR-CTC-1B.pt"
            ),
            encoding="utf-8",
        )
        os.environ["FAIRSEQ2_USER_ASSET_DIR"] = str(cards)
        import torch
        from fairseq2.data.tokenizers.hub import load_tokenizer
        from fairseq2.models.hub import load_model

        self.torch = torch
        self.device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        dtype = torch.bfloat16 if self.device.type == "cuda" else torch.float32
        self.model = load_model("ce_omniasr_ctc_1b", device=self.device, dtype=dtype).eval()
        self.dtype = dtype
        self.tokenizer = load_tokenizer("ce_omniasr_tokenizer_v1")
        self.encoder = self.tokenizer.create_encoder()
        info = self.tokenizer.vocab_info
        self.blank = 0  # fairseq2 wav2vec2 CTC: index 0 (`<s>`/pad) is the CTC blank [RV]
        self.unk = getattr(info, "unk_idx", None)
        separator = str(defaults.get("word_separator", " "))
        ids = [int(i) for i in self.encoder(separator).tolist()]
        self.separator: int | None = ids[-1] if ids else None

    def emissions(self, wav16k: str) -> tuple[np.ndarray, float]:
        import soundfile as sf
        from fairseq2.nn.batch_layout import BatchLayout
        from torch.nn.functional import layer_norm, log_softmax

        torch = self.torch
        samples, rate = sf.read(wav16k, dtype="float32")
        if rate != 16_000:
            raise ValueError(f"expected 16 kHz audio, got {rate}")
        waveform = torch.from_numpy(np.ascontiguousarray(samples))
        waveform = layer_norm(waveform, waveform.shape).to(self.device, self.dtype)[None, :]
        layout = BatchLayout(waveform.shape, seq_lens=[waveform.shape[1]], device=self.device)
        with torch.inference_mode():
            logits, out_layout = self.model(waveform, layout)
        frames = int(out_layout.seq_lens[0])
        log_probs = log_softmax(logits[0, :frames].float(), dim=-1).cpu().numpy()
        return log_probs, (len(samples) / 16_000.0) / max(frames, 1)

    def encode_words(self, words: list[str], language: str) -> list[list[int]]:
        out: list[list[int]] = []
        for word in words:
            ids = [int(i) for i in self.encoder(word).tolist()]
            out.append([i for i in ids if i not in (self.unk, self.blank, self.separator)])
        return out

    def close(self) -> None:
        self.model = None
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
