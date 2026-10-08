"""The real InfiniteTalk backend: imported only on a `wan` worker built by
`infra/docker/worker-wan.Dockerfile` (variant `infinitetalk`), where the InfiniteTalk repository at the
pinned commit is on `PYTHONPATH` with its pinned torch stack.

It mirrors `generate_infinitetalk.py` at commit 50aa0a9 (single person, `audio_mode=localfile`,
single GPU): `wan.InfiniteTalkPipeline` built once at load, chinese-wav2vec2 audio features on the
CPU, loudness normalization to −23 LUFS, then `generate_infinitetalk()` and `save_video_ffmpeg()`.

Status: implemented against the upstream code, **untested on a GPU** (rule 5) — the contract suite
runs the adapter with `testing.fake_backend` instead.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

from ce_plugin_avatar_infinitetalk.params import InfiniteTalkParams

__all__ = ["InfiniteTalkBackend"]


def _loudness_norm(speech: Any, sample_rate: int, lufs: float = -23.0) -> Any:
    import pyloudnorm as pyln

    meter = pyln.Meter(sample_rate)
    loudness = meter.integrated_loudness(speech)
    if abs(loudness) > 100:  # silence: leave it alone (upstream behavior)
        return speech
    return pyln.normalize.loudness(speech, loudness, lufs)


class InfiniteTalkBackend:
    def __init__(
        self,
        *,
        base_dir: Path,
        wav2vec_dir: Path,
        infinitetalk_file: Path,
        quant_file: Path | None,
        lora_file: Path | None,
        defaults: dict[str, Any],
    ) -> None:
        if (defaults.get("quant") or None) and (quant_file is None or not quant_file.is_file()):
            # the manifest's `files` fetch the bf16 checkpoint only: say so instead of failing deep in the load
            raise RuntimeError(
                f"quant={defaults.get('quant')!r} needs {quant_file}, which the model cache does not fetch: add "
                "quant_models/infinitetalk_single_fp8* to the infinitetalk-single model's files (and its size) or "
                "keep quant: null"
            )
        import torch
        import wan  # the InfiniteTalk repository
        from src.audio_analysis.wav2vec2 import Wav2Vec2Model  # the InfiniteTalk repository
        from transformers import Wav2Vec2FeatureExtractor
        from wan.configs import WAN_CONFIGS

        self.torch = torch
        quant = defaults.get("quant") or None
        self.pipeline = wan.InfiniteTalkPipeline(
            config=WAN_CONFIGS["infinitetalk-14B"],
            checkpoint_dir=str(base_dir),
            quant_dir=str(quant_file) if quant and quant_file is not None else None,
            device_id=0,
            rank=0,
            t5_fsdp=False,
            dit_fsdp=False,
            use_usp=False,
            t5_cpu=bool((defaults.get("low_memory") or {}).get("t5_cpu", False)),
            lora_dir=[str(lora_file)] if lora_file is not None and not quant else None,
            lora_scales=[float(defaults.get("lora_scale", 1.0))],
            quant=quant,
            dit_path=None,
            infinitetalk_dir=str(infinitetalk_file),
        )
        self._persistent: int | None = None
        self.feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(str(wav2vec_dir), local_files_only=True)
        self.audio_encoder = Wav2Vec2Model.from_pretrained(str(wav2vec_dir), local_files_only=True).to("cpu")
        self.audio_encoder.feature_extractor._freeze_parameters()

    def _vram(self, persistent: int | None) -> None:
        if persistent is not None and persistent != self._persistent:
            self.pipeline.vram_management = True
            self.pipeline.enable_vram_management(num_persistent_param_in_dit=persistent)
            self._persistent = persistent

    def _embedding(self, speech: Any, fps: int) -> Any:
        import numpy as np
        from einops import rearrange

        torch = self.torch
        video_length = len(speech) / 16000 * fps
        features = np.squeeze(self.feature_extractor(speech, sampling_rate=16000).input_values)
        tensor = torch.from_numpy(features).float().unsqueeze(0)
        with torch.no_grad():
            out = self.audio_encoder(tensor, seq_len=int(video_length), output_hidden_states=True)
        embedding = torch.stack(out.hidden_states[1:], dim=1).squeeze(0)
        return rearrange(embedding, "b s d -> s b d").cpu().detach()

    def generate(self, params: InfiniteTalkParams, image: Path, audio: Path, out_dir: Path) -> Path:
        import librosa
        import soundfile as sf
        from wan.utils.multitalk_utils import save_video_ffmpeg

        torch = self.torch
        self._vram(params.num_persistent_param_in_dit)
        speech, _ = librosa.load(str(audio), sr=16000)
        speech = _loudness_norm(speech, 16000)
        embedding_path = out_dir / "audio_embedding.pt"
        torch.save(self._embedding(speech, params.fps), embedding_path)
        wav16 = out_dir / "audio_16k.wav"
        sf.write(str(wav16), speech, 16000)
        clip = {
            "prompt": params.prompt,
            "cond_video": str(image),
            "cond_audio": {"person1": str(embedding_path)},
            "video_audio": str(wav16),
        }
        # the attributes generate_infinitetalk() reads from its argparse namespace (upstream defaults)
        extra = types.SimpleNamespace(
            use_teacache=False,
            teacache_thresh=0.2,
            size=params.size_bucket,
            use_apg=False,
            apg_momentum=-0.75,
            apg_norm_threshold=55,
            mode=params.mode,
        )
        video = self.pipeline.generate_infinitetalk(
            clip,
            size_buckget=params.size_bucket,
            motion_frame=params.motion_frame,
            frame_num=params.frame_num,
            shift=params.shift,
            sampling_steps=params.steps,
            text_guide_scale=params.text_guide_scale,
            audio_guide_scale=params.audio_guide_scale,
            n_prompt=params.negative,
            seed=params.seed,
            offload_model=True,
            max_frames_num=params.max_frames if params.mode == "streaming" else params.frame_num,
            color_correction_strength=params.color_correction_strength,
            extra_args=extra,
        )
        stem = out_dir / "infinitetalk_raw"
        save_video_ffmpeg(video, str(stem), [str(wav16)], high_quality_save=False)
        return stem.with_suffix(".mp4")

    def close(self) -> None:
        self.pipeline = None
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
