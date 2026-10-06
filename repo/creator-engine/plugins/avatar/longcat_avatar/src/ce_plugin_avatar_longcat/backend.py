"""The real LongCat-Video-Avatar 1.5 backend: imported only on a `wan` worker built with the
`longcat_avatar` variant of `infra/docker/worker-wan.Dockerfile` (the LongCat-Video repository at the
pinned commit on `PYTHONPATH`, torch 2.6).

It mirrors `run_demo_avatar_single_audio_to_video.py` at commit 6b3f4b8 with `--stage_1=ai2v
--model_type avatar-v1.5 --use_distill --use_int8` on one GPU (context-parallel size 1), except that
the vocal separator is skipped: the input is the synthesized dialogue stem (DECISIONS D91).

Status: implemented against the upstream code, **untested on a GPU** (rule 5).
"""

from __future__ import annotations

import math
import os
import socket
from pathlib import Path
from typing import Any

from ce_plugin_avatar_longcat.params import LongCatParams

__all__ = ["LongCatAvatarBackend"]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LongCatAvatarBackend:
    def __init__(self, *, checkpoint_dir: Path, base_dir: Path, defaults: dict[str, Any]) -> None:
        import torch
        import torch.distributed as dist
        from longcat_video.audio_process import get_audio_encoder, get_audio_feature_extractor
        from longcat_video.context_parallel import context_parallel_util
        from longcat_video.modules.autoencoder_kl_wan import AutoencoderKLWan
        from longcat_video.modules.avatar.longcat_video_dit_avatar import LongCatVideoAvatarTransformer3DModel
        from longcat_video.modules.quantization import load_quantized_dit
        from longcat_video.modules.scheduling_flow_match_euler_discrete import FlowMatchEulerDiscreteScheduler
        from longcat_video.pipeline_longcat_video_avatar import LongCatVideoAvatarPipeline
        from transformers import AutoTokenizer, UMT5EncoderModel

        self.torch = torch
        model_type = str(defaults.get("model_type", "avatar-v1.5"))
        self.model_type = model_type
        if not dist.is_initialized():  # the pipeline's context-parallel utilities need a process group
            os.environ.setdefault("MASTER_ADDR", "127.0.0.1")
            os.environ.setdefault("MASTER_PORT", str(_free_port()))
            dist.init_process_group(backend="nccl", rank=0, world_size=1)
        torch.cuda.set_device(0)
        context_parallel_util.init_context_parallel(context_parallel_size=1, global_rank=0, world_size=1)
        split = context_parallel_util.get_optimal_split(context_parallel_util.get_cp_size())
        bf16 = torch.bfloat16
        tokenizer = AutoTokenizer.from_pretrained(str(base_dir), subfolder="tokenizer", torch_dtype=bf16)
        text_encoder = UMT5EncoderModel.from_pretrained(str(base_dir), subfolder="text_encoder", torch_dtype=bf16)
        vae = AutoencoderKLWan.from_pretrained(str(base_dir), subfolder="vae", torch_dtype=bf16)
        scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(
            str(checkpoint_dir), subfolder="scheduler", torch_dtype=bf16
        )
        if defaults.get("use_int8", True):
            dit = load_quantized_dit(str(checkpoint_dir), subfolder="base_model_int8", cp_split_hw=split)
        else:
            dit = LongCatVideoAvatarTransformer3DModel.from_pretrained(
                str(checkpoint_dir), subfolder="base_model", cp_split_hw=split, torch_dtype=bf16
            )
        if defaults.get("use_distill", True):
            lora = Path(checkpoint_dir) / "lora" / "dmd_lora.safetensors"
            dit.load_lora(str(lora), "dmd", multiplier=1.0, lora_network_dim=128, lora_network_alpha=64)
            dit.enable_loras(["dmd"])
        whisper = str(Path(checkpoint_dir) / "whisper-large-v3")
        self.pipe = LongCatVideoAvatarPipeline(
            tokenizer=tokenizer,
            text_encoder=text_encoder,
            vae=vae,
            scheduler=scheduler,
            dit=dit,
            audio_encoder=get_audio_encoder(whisper, model_type).to(0),
            audio_feature_extractor=get_audio_feature_extractor(whisper, model_type),
            model_type=model_type,
        )
        self.pipe.to(0)

    def _frames(self, output: Any) -> list[Any]:
        import numpy as np
        import PIL.Image

        frames = output[0]
        return [PIL.Image.fromarray((frames[i] * 255).astype(np.uint8)) for i in range(frames.shape[0])]

    def generate(self, params: LongCatParams, image: Path, audio: Path, out_dir: Path) -> Path:
        import librosa
        import numpy as np
        from diffusers.utils import load_image
        from longcat_video.audio_process.torch_utils import save_video_ffmpeg

        torch = self.torch
        generator = torch.Generator(device=0)
        generator.manual_seed(params.seed)
        audio_stride = 1  # avatar-v1.5 at 25 fps (upstream)
        duration = params.total_frames / params.fps
        speech, sr = librosa.load(str(audio), sr=16000)
        missing = math.ceil((duration - len(speech) / sr) * sr)
        if missing > 0:
            speech = np.append(speech, [0.0] * missing)
        full = self.pipe.get_audio_embedding(
            speech, fps=params.fps * audio_stride, device=0, sample_rate=sr, model_type=self.model_type
        )
        if torch.isnan(full).any():
            raise ValueError("broken audio embedding (NaN values)")
        offsets = torch.arange(2 * 2 + 1) - 2

        def window(start: int) -> Any:
            centers = torch.arange(start, start + audio_stride * params.window_frames, audio_stride).unsqueeze(1)
            idx = torch.clamp(centers + offsets.unsqueeze(0), min=0, max=full.shape[0] - 1)
            return full[idx][None, ...].to(0)

        common = {
            "prompt": params.prompt,
            "negative_prompt": params.negative,
            "num_frames": params.window_frames,
            "num_inference_steps": params.steps,
            "text_guidance_scale": params.text_guidance_scale,
            "audio_guidance_scale": params.audio_guidance_scale,
            "generator": generator,
            "output_type": "both",
            "use_distill": params.use_distill,
        }
        output, latent = self.pipe.generate_ai2v(
            image=load_image(str(image)), resolution=params.resolution, audio_emb=window(0), **common
        )
        video = self._frames(output)
        width, height = video[0].size
        ref_latent = latent[:, :, :1].clone()
        frames = list(video)
        start = 0
        for _ in range(1, params.segments):
            start += audio_stride * (params.window_frames - params.cond_frames)
            output, latent = self.pipe.generate_avc(
                video=video,
                video_latent=latent,
                height=height,
                width=width,
                num_cond_frames=params.cond_frames,
                use_kv_cache=True,
                offload_kv_cache=False,
                enhance_hf=not params.use_distill,
                audio_emb=window(start),
                ref_latent=ref_latent,
                ref_img_index=params.ref_img_index,
                mask_frame_range=params.mask_frame_range,
                **common,
            )
            video = self._frames(output)
            frames.extend(video[params.cond_frames :])
        stem = out_dir / "longcat_raw"
        tensor = torch.from_numpy(np.array(frames))
        save_video_ffmpeg(tensor, str(stem), str(audio), fps=params.fps, quality=5)
        return stem.with_suffix(".mp4")

    def close(self) -> None:
        self.pipe = None
        if self.torch.cuda.is_available():
            self.torch.cuda.empty_cache()
