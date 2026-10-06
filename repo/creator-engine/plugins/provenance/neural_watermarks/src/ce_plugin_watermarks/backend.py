"""The real VideoSeal and AudioSeal backends (`videoseal` 1.0.1 with the VideoSeal 0.0 96-bit
checkpoint, `audioseal` 0.2.0 with the 16-bit generator and detector), imported only on a `post`
worker. Checkpoints are loaded from the model cache; the packages' own model cards supply the
architectures (`videoseal_0.0`, `audioseal_wm_16bits`, `audioseal_detector_16bits`). **Untested on a GPU** (rule 5)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["AudioSealBackend", "VideoSealBackend"]


class VideoSealBackend:
    mode = "real"

    def __init__(self, *, checkpoint: Path) -> None:
        import torch
        import videoseal
        from omegaconf import OmegaConf
        from videoseal.utils.cfg import setup_model

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        # the packaged `videoseal_0.0` card (architecture inline, no attenuation), the local checkpoint
        config = OmegaConf.load(Path(videoseal.__file__).parent / "cards" / "videoseal_0.0.yaml")
        if int(config.args.nbits) != 96:
            raise RuntimeError(f"expected the 96-bit VideoSeal 0.0 card, got {config.args.nbits} bits")
        self.model = setup_model(config, checkpoint).eval().to(self.device)

    def _tensor(self, frames: np.ndarray) -> Any:
        return self.torch.from_numpy(np.ascontiguousarray(frames)).permute(0, 3, 1, 2).float().div(255.0)

    def embed(self, frames: np.ndarray, bits: np.ndarray) -> list[np.ndarray]:
        msg = self.torch.from_numpy(bits.astype(np.float32))[None, :].to(self.device)
        with self.torch.no_grad():
            out = self.model.embed(self._tensor(frames), msgs=msg, is_video=True)["imgs_w"]
        return list(out.clamp(0, 1).mul(255).round().byte().permute(0, 2, 3, 1).cpu().numpy())

    def decode(self, frames: np.ndarray) -> np.ndarray:
        with self.torch.no_grad():
            msg = self.model.extract_message(self._tensor(frames), aggregation="avg")
        return msg[0].int().cpu().numpy().astype(np.uint8)

    def close(self) -> None:
        self.model = None


def _load_audioseal(card: str, checkpoint: Path, config_type: Any, create: Any) -> Any:
    from audioseal.loader import (
        AudioSeal,
        convert_state_dict_for_scriptable_model,
        load_local_model_config,
        load_model_checkpoint,
        load_state_dict,
    )
    from omegaconf import OmegaConf

    config = OmegaConf.to_container(load_local_model_config(card))
    assert isinstance(config, dict)
    config.pop("checkpoint", None)
    state = load_model_checkpoint(str(checkpoint))
    if "xp.cfg" in state:
        config = {**state["xp.cfg"], **config}
    model_config = AudioSeal.parse_config(config, config_type=config_type)
    if "best_state" in state:
        state = state["best_state"]["model"]
    elif "model" in state:
        state = state["model"]
    model = create(model_config)
    load_state_dict(model, state_dict=convert_state_dict_for_scriptable_model(state))
    return model


class AudioSealBackend:
    mode = "real"

    def __init__(self, *, generator: Path, detector: Path) -> None:
        import torch
        from audioseal.builder import AudioSealDetectorConfig, AudioSealWMConfig, create_detector, create_generator

        self.torch = torch
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.generator = (
            _load_audioseal("audioseal_wm_16bits", generator, AudioSealWMConfig, create_generator)
            .to(self.device)
            .eval()
        )
        self.detector = (
            _load_audioseal("audioseal_detector_16bits", detector, AudioSealDetectorConfig, create_detector)
            .to(self.device)
            .eval()
        )

    def watermark_signal(self, mono16k: np.ndarray, bits: np.ndarray) -> np.ndarray:
        x = self.torch.from_numpy(np.ascontiguousarray(mono16k, dtype=np.float32))[None, None, :].to(self.device)
        msg = self.torch.from_numpy(bits.astype(np.int64))[None, :].to(self.device)
        with self.torch.no_grad():
            wm = self.generator.get_watermark(x, message=msg)
        return wm[0, 0].float().cpu().numpy()

    def detect(self, mono16k: np.ndarray) -> tuple[float, np.ndarray]:
        x = self.torch.from_numpy(np.ascontiguousarray(mono16k, dtype=np.float32))[None, None, :].to(self.device)
        with self.torch.no_grad():
            probability, message = self.detector.detect_watermark(x)
        return float(probability[0]), message[0].int().cpu().numpy().astype(np.uint8)

    def close(self) -> None:
        self.generator = self.detector = None
