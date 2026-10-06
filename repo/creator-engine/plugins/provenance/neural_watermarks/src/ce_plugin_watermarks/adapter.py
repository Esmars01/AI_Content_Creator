"""VideoSeal / AudioSeal adapters (`provenance.watermark_video`, `provenance.watermark_audio`,
`provenance.verify`; §27).

Video: frames are streamed in chunks (`iter_video_frames`), watermarked with the payload's 96 bits
and re-encoded (H.264 CRF 16; the watermark is designed to survive compression); the audio stream
is copied untouched (the audio layer handles the mix). Audio: the mix is downmixed to 16 kHz mono,
AudioSeal computes the watermark signal there, and the upsampled watermark is added to every
channel of the 48 kHz mix, which is re-encoded as AAC in an M4A (the signer muxes it with the
watermarked frames). Verification decodes the bits and reports bit accuracy and the decoded
payload; `present` needs the configured accuracy (and, for audio, the detector's probability).

`mode` comes from the backend: `real` for the real models, `mock_dev` for the CPU stand-ins (a
test backend never yields a `real` provenance layer)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from ce_contracts import models as m
from ce_contracts.common import LoadContext, RunContext
from ce_plugin_kit.engine import EngineAdapter
from ce_plugin_kit.media import ffmpeg, frames_to_mp4, iter_video_frames, probe, read_audio, write_wav

from ce_plugin_watermarks.payload import bit_accuracy, bits_of, hex_of

__all__ = ["AudioSealAdapter", "VideoSealAdapter"]


def _mode(backend: Any) -> Literal["real", "mock_dev"]:
    """`mock_dev` only when the backend says so (the CPU stand-ins); anything else is the real model."""
    return "mock_dev" if getattr(backend, "mode", "real") == "mock_dev" else "real"


class VideoSealAdapter(EngineAdapter):
    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_watermarks.backend import VideoSealBackend

        assert self.paths is not None
        return VideoSealBackend(checkpoint=self.paths.model("videoseal-0.0") / "checkpoint.pth")

    async def run_provenance_watermark_video(self, request: m.WatermarkRequest, ctx: RunContext) -> m.WatermarkResult:
        backend = self.require_backend()
        bits = bits_of(request.payload_id, 96)
        work = self.scratch(ctx, "wm-video")
        source = await ctx.read_artifact(request.media)
        info = probe(source)
        chunk = int(self.defaults.get("chunk_frames", 64))

        def frames() -> Any:
            for block in iter_video_frames(source, chunk=chunk):
                ctx.cancel.raise_if_cancelled()
                yield from backend.embed(block, bits)

        video_only = frames_to_mp4(
            frames(), work / "frames_wm.mp4", info.fps or 25.0, crf=int(self.defaults.get("crf", 16))
        )
        out = work / "watermarked.mp4"
        if info.has_audio:
            ffmpeg(
                "-i", str(video_only), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-movflags", "+faststart", str(out),
            )  # fmt: skip
        else:
            out = video_only
        mode = _mode(backend)
        ref = await ctx.write_artifact(
            out, "video", {"adapter_id": self.manifest.id, "watermark": mode, "payload_id": request.payload_id},
            role="media", mime="video/mp4",
        )  # fmt: skip
        return m.WatermarkResult(media=ref, payload_id=request.payload_id, mode=mode)

    async def run_provenance_verify(self, request: m.VerifyRequest, ctx: RunContext) -> m.VerifyResult:
        backend = self.require_backend()
        if request.layer != "watermark_video":
            return _other_layer(self, request, backend)
        source = await ctx.read_artifact(request.media)
        limit = int(self.defaults.get("verify_frames", 96))
        blocks, seen = [], 0
        for block in iter_video_frames(source, chunk=min(limit, 32)):
            blocks.append(block[: limit - seen])
            seen += len(blocks[-1])
            if seen >= limit:
                break
        decoded = np.asarray(await self.call(ctx, backend.decode, np.concatenate(blocks)), dtype=np.uint8)
        payload = hex_of(decoded)
        detail: dict[str, Any] = {"frames": seen, "decoded_payload_id": payload}
        present = False
        if request.payload_id:
            accuracy = bit_accuracy(decoded, bits_of(request.payload_id, 96))
            detail["bit_accuracy"] = round(accuracy, 4)
            present = accuracy >= float(self.defaults.get("min_bit_accuracy", 0.9))
        mode = _mode(backend)
        return m.VerifyResult(layer="watermark_video", present=present, mode=mode, payload_id=payload, detail=detail)


class AudioSealAdapter(EngineAdapter):
    rate = 16_000

    def create_backend(self, ctx: LoadContext) -> Any:
        from ce_plugin_watermarks.backend import AudioSealBackend

        assert self.paths is not None
        root = self.paths.model("audioseal")
        return AudioSealBackend(generator=root / "generator_base.pth", detector=root / "detector_base.pth")

    async def run_provenance_watermark_audio(self, request: m.WatermarkRequest, ctx: RunContext) -> m.WatermarkResult:
        backend = self.require_backend()
        bits = bits_of(request.payload_id, 16)
        work = self.scratch(ctx, "wm-audio")
        source = await ctx.read_artifact(request.media)
        info = probe(source)
        if not info.has_audio:
            raise ValueError("provenance.watermark_audio: the media has no audio")
        rate, channels = info.sample_rate or 48_000, max(1, info.channels or 2)
        mix = read_audio(source, rate, channels=channels).reshape(-1, channels)
        mono16 = read_audio(source, self.rate)
        signal16 = np.asarray(await self.call(ctx, backend.watermark_signal, mono16, bits), dtype=np.float32)
        signal = _resample(signal16, self.rate, rate, len(mix))
        marked = np.clip(mix + signal[:, None], -1.0, 1.0)
        wav = write_wav(work / "marked.wav", marked, rate)
        out = work / "watermarked_audio.m4a"
        ffmpeg("-i", str(wav), "-c:a", "aac", "-b:a", str(self.defaults.get("aac_bitrate", "256k")), str(out))
        mode = _mode(backend)
        ref = await ctx.write_artifact(
            out, "audio", {"adapter_id": self.manifest.id, "watermark": mode, "payload_id": request.payload_id},
            role="media", mime="audio/mp4",
        )  # fmt: skip
        return m.WatermarkResult(media=ref, payload_id=request.payload_id, mode=mode)

    async def run_provenance_verify(self, request: m.VerifyRequest, ctx: RunContext) -> m.VerifyResult:
        backend = self.require_backend()
        if request.layer != "watermark_audio":
            return _other_layer(self, request, backend)
        source = await ctx.read_artifact(request.media)
        mono16 = read_audio(source, self.rate)
        probability, decoded = await self.call(ctx, backend.detect, mono16)
        decoded = np.asarray(decoded, dtype=np.uint8)
        detail: dict[str, Any] = {
            "detection_probability": round(float(probability), 4),
            "decoded_bits": "".join(map(str, decoded)),
        }
        present = float(probability) >= float(self.defaults.get("min_detection", 0.5))
        if request.payload_id:
            accuracy = bit_accuracy(decoded, bits_of(request.payload_id, 16))
            detail["bit_accuracy"] = round(accuracy, 4)
            present = present and accuracy >= float(self.defaults.get("min_bit_accuracy", 0.875))
        mode = _mode(backend)
        return m.VerifyResult(
            layer="watermark_audio", present=present, mode=mode, payload_id=request.payload_id, detail=detail
        )


def _other_layer(adapter: EngineAdapter, request: m.VerifyRequest, backend: Any) -> m.VerifyResult:
    """A layer this verifier does not read: reported absent with the reason (never a false 'present')."""
    own = "watermark_video" if isinstance(adapter, VideoSealAdapter) else "watermark_audio"
    return m.VerifyResult(
        layer=request.layer, present=False, mode=_mode(backend),
        payload_id=request.payload_id, detail={"reason": f"{adapter.manifest.id} verifies {own} only"},
    )  # fmt: skip


def _resample(signal: np.ndarray, src: int, dst: int, length: int) -> np.ndarray:
    """Linear-interpolated upsampling of the (band-limited, 16 kHz) watermark signal to the mix rate."""
    if src == dst:
        out = signal
    else:
        t_dst = np.arange(length) / dst
        out = np.interp(t_dst, np.arange(len(signal)) / src, signal).astype(np.float32)
    if len(out) < length:
        out = np.concatenate([out, np.zeros(length - len(out), dtype=np.float32)])
    return out[:length]
