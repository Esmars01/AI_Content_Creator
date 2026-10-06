#!/usr/bin/env python3
"""Generates the media of the smoke golden set (`eval/smoke/media/`, Phase 8).

- `clip_small.mp4`: the portrait keyframe as a 2 s, 256×448, 12 fps clip with a slow zoom (FFmpeg
  only; deterministic) — the input of upscaling, interpolation, watermarking and video QC cases.
- `talking_still_4s.mp4`: the portrait at 480×832, 25 fps, muxed with `speech_en_4s.wav` — the
  input of lip-sync and VLM cases.
- `speech_ref_en.wav`: the reference sentence (`REFERENCE_EN`) spoken by the Kokoro CPU engine
  (`--speech`, needs `make fetch-cpu-assets`); `speech_ref_tr.wav` and `speech_ref_ar.wav` spoken by
  eSpeak NG (`--speech`, needs `espeak-ng`). Kokoro is dev-only because of its GPL phonemizer
  (ADR 0043); the generated audio is our own synthetic output and carries no third-party content.

The committed files are the reference: `eval/smoke/cases.yaml` pins their sha256 and the harness
verifies them before a run. Re-running regenerates the FFmpeg clips byte for byte on the same FFmpeg
build; Kokoro output may differ across library versions, so `speech_ref_en.wav` is regenerated only
on request.

Usage: uv run python scripts/gen_smoke_media.py [--speech] [--print-sha]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MEDIA = ROOT / "eval" / "smoke" / "media"
REFERENCE_EN = "The quick brown fox jumps over the lazy dog, and then it takes a short nap."
# Turkish and Arabic references for the tr/ar aligner and ASR cases, spoken by eSpeak NG (formant
# synthesis: robotic, but real phonemes with a known transcript; the tool is GPL, its output audio
# is not a derivative work of it). Owner-supplied recordings can replace them (README).
REFERENCES_ESPEAK = {
    "tr": "Bugün size kısa bir hikâye anlatacağım ve sonra bir soru soracağım.",
    "ar": "اليوم سأحكي لكم قصة قصيرة ثم أطرح عليكم سؤالا.",
}


def ffmpeg(*args: str) -> None:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise SystemExit("ffmpeg is required")
    subprocess.run([binary, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def clips() -> None:
    portrait = MEDIA / "portrait_keyframe.png"
    ffmpeg(
        "-loop", "1", "-i", str(portrait),
        "-vf", "scale=512:896,zoompan=z='1+0.002*on':d=24:s=256x448:fps=12,format=yuv420p",
        "-frames:v", "24", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-bitexact",
        "-flags:v", "+bitexact", "-map_metadata", "-1", str(MEDIA / "clip_small.mp4"),
    )  # fmt: skip
    ffmpeg(
        "-loop", "1", "-framerate", "25", "-i", str(portrait), "-i", str(MEDIA / "speech_en_4s.wav"),
        "-vf", "scale=480:832,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "22",
        "-c:a", "aac", "-b:a", "96k", "-shortest", "-bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
        str(MEDIA / "talking_still_4s.mp4"),
    )  # fmt: skip


async def speech() -> None:
    from ce_contracts import models as m
    from ce_contracts.common import LoadContext
    from ce_contracts.local import LocalRunContext
    from ce_contracts.plugins import discover

    plugin = discover(app_env="dev", include_mocks=False).get("kokoro_cpu")
    adapter = plugin.adapter()
    with tempfile.TemporaryDirectory() as tmp:
        ctx = LocalRunContext(Path(tmp), seed=7)
        await adapter.load(
            LoadContext(
                model_cache_dir=str(ROOT / ".cache" / "models"), scratch_dir=str(ctx.scratch_dir), app_env="dev"
            )
        )
        result = await adapter.run(
            "voice.tts", m.TTSRequest(text=REFERENCE_EN, words=REFERENCE_EN.split(), language="en"), ctx
        )
        path = await ctx.read_artifact(result.audio)
        ffmpeg("-i", str(path), "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(MEDIA / "speech_ref_en.wav"))
        await adapter.unload()


def espeak() -> None:
    binary = shutil.which("espeak-ng")
    if binary is None:
        raise SystemExit("espeak-ng is required for --speech (apt-get install espeak-ng)")
    with tempfile.TemporaryDirectory() as tmp:
        for language, text in REFERENCES_ESPEAK.items():
            raw = Path(tmp) / f"{language}.wav"
            subprocess.run([binary, "-v", language, "-s", "140", "-w", str(raw), text], check=True)
            ffmpeg("-i", str(raw), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", "-bitexact", "-map_metadata", "-1",
                   str(MEDIA / f"speech_ref_{language}.wav"))  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--speech", action="store_true", help="also regenerate speech_ref_en.wav with Kokoro")
    parser.add_argument("--print-sha", action="store_true", help="print the sha256 of every media file")
    args = parser.parse_args()
    clips()
    if args.speech:
        asyncio.run(speech())
        espeak()
    if args.print_sha:
        for path in sorted(MEDIA.iterdir()):
            print(f"{path.name}: {hashlib.sha256(path.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
