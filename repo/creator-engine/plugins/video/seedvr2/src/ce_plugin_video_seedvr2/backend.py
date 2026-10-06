"""The real SeedVR2-3B backend: mirrors `projects/inference_seedvr2_3b.py` at commit e4de8c2 (one
sampling step, CFG 1.0, the shipped positive/negative text embeddings, wavelet color fix) on one GPU
(sequence parallel size 1), for one chunk of frames at a time. Imported only on a `post` worker
whose image has the SeedVR repository on `PYTHONPATH` plus apex and flash-attn.

Status: implemented against the upstream code, **untested on a GPU** (rule 5)."""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["SeedVR2Backend"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class SeedVR2Backend:
    def __init__(self, *, weights_dir: Path, code_dir: Path, defaults: dict[str, Any]) -> None:
        for key, value in (("RANK", "0"), ("LOCAL_RANK", "0"), ("WORLD_SIZE", "1"), ("MASTER_ADDR", "127.0.0.1")):
            os.environ.setdefault(key, value)
        os.environ.setdefault("MASTER_PORT", str(_free_port()))
        import datetime

        import torch
        import torch.distributed as dist
        from common.config import load_config
        from common.distributed import init_torch
        from omegaconf import OmegaConf
        from projects.video_diffusion_sr.infer import VideoDiffusionInfer

        self.torch = torch
        weights = Path(weights_dir)
        config = load_config(str(Path(code_dir) / "configs_3b" / "main.yaml"))
        runner = VideoDiffusionInfer(config)
        OmegaConf.set_readonly(runner.config, False)
        if not dist.is_initialized():
            init_torch(cudnn_benchmark=False, timeout=datetime.timedelta(seconds=3600))
        runner.configure_dit_model(device="cuda", checkpoint=str(weights / "seedvr2_ema_3b.pth"))
        runner.config.vae.checkpoint = str(weights / "ema_vae.pth")
        runner.configure_vae_model()
        if hasattr(runner.vae, "set_memory_limit"):
            runner.vae.set_memory_limit(**runner.config.vae.memory_limit)
        runner.config.diffusion.cfg.scale = 1.0
        runner.config.diffusion.cfg.rescale = 0.0
        runner.config.diffusion.timesteps.sampling.steps = 1
        runner.configure_diffusion()
        self.runner = runner
        self.pos = torch.load(str(weights / "pos_emb.pt"))
        self.neg = torch.load(str(weights / "neg_emb.pt"))

    def restore(self, frames: list[np.ndarray], area: float, seed: int) -> list[np.ndarray]:
        from common.distributed import get_device
        from common.seed import set_seed
        from data.image.transforms.divisible_crop import DivisibleCrop
        from data.image.transforms.na_resize import NaResize
        from data.video.transforms.rearrange import Rearrange
        from einops import rearrange
        from projects.video_diffusion_sr.color_fix import wavelet_reconstruction
        from torchvision.transforms import Compose, Lambda, Normalize

        torch, runner = self.torch, self.runner
        set_seed(seed, same_across_ranks=True)
        device = get_device()
        transform = Compose([
            NaResize(resolution=area, mode="area", downsample_only=False),
            Lambda(lambda x: torch.clamp(x, 0.0, 1.0)),
            DivisibleCrop((16, 16)),
            Normalize(0.5, 0.5),
            Rearrange("t c h w -> c t h w"),
        ])  # fmt: skip
        video = torch.from_numpy(np.stack(frames)).permute(0, 3, 1, 2).float() / 255.0
        cond = transform(video.to(device))
        length = cond.size(1)
        padded = cond
        if length > 1 and (length - 1) % 4:
            pad = 4 - (length - 1) % 4
            padded = torch.cat([cond, cond[:, -1:].repeat(1, pad, 1, 1)], dim=1)
        runner.dit.to("cpu")
        runner.vae.to(device)
        (latent,) = runner.vae_encode([padded])
        runner.vae.to("cpu")
        runner.dit.to(device)
        noise, aug = torch.randn_like(latent), torch.randn_like(latent)
        condition = runner.get_condition(
            noise, task="sr", latent_blur=runner.schedule.forward(latent, aug, torch.tensor([0.0], device=device))
        )
        with torch.no_grad(), torch.autocast("cuda", torch.bfloat16, enabled=True):
            (sample,) = runner.inference(
                noises=[noise], conditions=[condition], dit_offload=True,
                texts_pos=[self.pos.to(device)], texts_neg=[self.neg.to(device)],
            )  # fmt: skip
        runner.dit.to("cpu")
        sample = rearrange(sample, "c t h w -> t c h w")[:length]
        reference = rearrange(cond, "c t h w -> t c h w")
        sample = wavelet_reconstruction(sample.to("cpu"), reference[: sample.size(0)].to("cpu"))
        sample = rearrange(sample, "t c h w -> t h w c").clip(-1, 1).mul_(0.5).add_(0.5).mul_(255).round()
        return list(sample.to(torch.uint8).numpy())

    def close(self) -> None:
        self.runner = None
        self.torch.cuda.empty_cache()
