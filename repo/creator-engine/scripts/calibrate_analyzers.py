#!/usr/bin/env python3
"""Calibrates the observation proxies of the real CPU analyzers (§16.2, Phase 7) and stores the
measured reliability per analyzer revision (`model_behavior_profiles`, ADR 0045).

- Visual proxies (look_away, smile_score, blink_event, nod, lean_in, …) on the owner-supplied
  fixture clips in `eval/fixtures/analyzer_clips/manifest.yaml` (consented or permissively licensed,
  §16.7, §41). Without them this part skips with that reason.
- Speech-rate proxies (speech_rate_slow / speech_rate_fast) on synthetic speech: the CPU TTS speaks
  each calibration sentence at known relative rates; faster-whisper times the words; the prosody
  analyzer measures the rate against the same sentence at rate 1.0.

Usage: uv run python scripts/calibrate_analyzers.py [--dry-run] [--json PATH] [--fixtures DIR]
       uv run python scripts/calibrate_analyzers.py --load eval/calibration/speech-v1.json
       (stores rows measured earlier, e.g. the recorded Phase 7 run, without measuring again)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SENTENCES = [
    "Most people think productivity is about doing more.",
    "Here is the thing nobody tells you about habits.",
    "The best teams I worked with wrote everything down.",
    "Start small, measure it, and keep the parts that work.",
    "If it takes two minutes, do it right away.",
    "Your calendar shows what you actually care about.",
]
RATES = [0.75, 0.85, 0.95, 1.0, 1.1, 1.25]


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip())


async def _adapter(registry: Any, plugin_id: str, cache: Path, scratch: Path) -> Any:
    from ce_contracts.common import LoadContext

    adapter = registry.get(plugin_id).adapter()
    await adapter.load(LoadContext(model_cache_dir=str(cache), scratch_dir=str(scratch / plugin_id), app_env="dev"))
    return adapter


def _revision(registry: Any, plugin_id: str) -> str:
    manifest = registry.get(plugin_id).manifest
    return manifest.primary_model.source.revision if manifest.primary_model else manifest.version


async def visual(
    registry: Any, vocab: Any, judge: Any, cfg: dict[str, Any], cache: Path, fixtures: Path, scratch: Path
) -> tuple[list[dict[str, Any]], str]:
    from ce_behavior.calibration import LabelledEvent, ProxyCalibration, proxy_scores, reliability_class
    from ce_contracts import models as m
    from ce_contracts.local import LocalRunContext

    manifest_path = fixtures / "manifest.yaml"
    if not manifest_path.is_file():
        return (
            [],
            f"visual proxies skipped: no fixture clips at {manifest_path} "
            "(owner-supplied, consented or permissively licensed; §16.7, §41)",
        )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    face = await _adapter(registry, "mediapipe_face", cache, scratch)
    body = await _adapter(registry, "mediapipe_body", cache, scratch)
    ctx = LocalRunContext(scratch / "visual")
    clips = []
    for clip in manifest.get("clips", []):
        ref = await ctx.put_file(fixtures / clip["file"], "video")
        request = m.MediaAnalysisRequest(media=ref, sample_hz=10.0)
        f = await face.run("face.landmarks", request, ctx)
        b = await body.run("body.landmarks", request, ctx)
        series = {**f.series, **b.series}
        duration = max((len(v) for v in series.values()), default=0) / 10.0
        events = [
            LabelledEvent(str(e["proxy"]), float(e["start_s"]), float(e["end_s"])) for e in clip.get("events", [])
        ]
        clips.append((series, 10.0, duration, events))
    rows = []
    for key, proxy in vocab.proxies.items():
        if proxy.analyzer not in ("face.landmarks", "body.landmarks"):
            continue
        tp, fp, fn, tn = proxy_scores(key, proxy, clips, judge)
        if tp + fn == 0:
            continue  # no labelled events for this proxy
        adapter_id = "mediapipe_face" if proxy.analyzer == "face.landmarks" else "mediapipe_body"
        result = ProxyCalibration(
            key,
            proxy.analyzer,
            adapter_id,
            _revision(registry, adapter_id),
            tp,
            fp,
            fn,
            tn,
            "low",
            "fixture_clips",
            str(manifest.get("set", "unnamed")),
        )
        result = ProxyCalibration(**{**result.__dict__, "reliability": reliability_class(result.f1, result.n, **cfg)})
        rows.append(
            {"adapter_id": adapter_id, "revision": result.revision, "proxy": key, "measured": result.measured()}
        )
    return rows, f"visual proxies: {len(rows)} calibrated on {len(clips)} clips"


async def speech(
    registry: Any, vocab: Any, cfg: dict[str, Any], cache: Path, scratch: Path
) -> tuple[list[dict[str, Any]], str]:
    from ce_behavior.calibration import ProxyCalibration, reliability_class, speech_rate_scores
    from ce_contracts import models as m
    from ce_contracts.local import LocalRunContext
    from ce_contracts.manifest import missing_assets
    from ce_core.text import tokenize

    for plugin_id in ("kokoro_cpu", "faster_whisper_cpu"):
        missing = missing_assets(registry.get(plugin_id).manifest, cache)
        if missing:
            return (
                [],
                f"speech-rate proxies skipped: {plugin_id} assets missing ({missing[0]}); run `make fetch-cpu-assets`",
            )
    tts = await _adapter(registry, "kokoro_cpu", cache, scratch)
    asr = await _adapter(registry, "faster_whisper_cpu", cache, scratch)
    prosody = await _adapter(registry, "prosody_features", cache, scratch)
    ctx = LocalRunContext(scratch / "speech", seed=0)
    trials: list[tuple[float, float, float]] = []
    base_wpm = float(registry.get("kokoro_cpu").manifest.defaults["base_wpm"])
    for sentence in SENTENCES:
        words = [t.text for t in tokenize(sentence)]
        measured: dict[float, float] = {}
        for rate in RATES:
            audio = await tts.run(
                "voice.tts",
                m.TTSRequest(text=sentence, words=words, language="en", wpm=base_wpm, engine={"rate": rate}),
                ctx,
            )
            heard = await asr.run("asr.transcribe", m.TranscribeRequest(audio=audio.audio, language="en"), ctx)
            result = await prosody.run(
                "audio.prosody", m.AudioAnalysisRequest(audio=audio.audio, word_timings=heard.words), ctx
            )
            if result.speech_rate_wpm:
                measured[rate] = float(result.speech_rate_wpm)
        baseline = measured.get(1.0)
        if baseline:
            trials += [(rate, wpm, baseline) for rate, wpm in measured.items() if rate != 1.0]
    slow = float(vocab.proxies["speech_rate_slow"].thresholds.get("relative_rate", 0.9))
    fast = float(vocab.proxies["speech_rate_fast"].thresholds.get("relative_rate", 1.0))
    rows = []
    for key, (tp, fp, fn, tn) in speech_rate_scores(trials, slow_threshold=slow, fast_threshold=fast).items():
        result = ProxyCalibration(
            key,
            "audio.prosody",
            "prosody_features",
            _revision(registry, "prosody_features"),
            tp,
            fp,
            fn,
            tn,
            "low",
            "synthetic_speech:kokoro_cpu+faster_whisper_cpu",
            "speech-v1",
        )
        result = ProxyCalibration(**{**result.__dict__, "reliability": reliability_class(result.f1, result.n, **cfg)})
        rows.append(
            {"adapter_id": "prosody_features", "revision": result.revision, "proxy": key, "measured": result.measured()}
        )
    return rows, f"speech-rate proxies: {len(rows)} calibrated on {len(trials)} synthetic trials"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="measure and print; store nothing")
    parser.add_argument("--json", type=Path, help="also write the rows to this file")
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=Path(os.environ.get("FIXTURE_CLIPS_DIR") or ROOT / "eval" / "fixtures" / "analyzer_clips"),
    )
    parser.add_argument(
        "--load", type=Path, action="append", default=[], help="store rows from a JSON file written by --json"
    )
    args = parser.parse_args(argv)
    _load_env()
    if args.load:
        return await _store([row for path in args.load for row in json.loads(path.read_text(encoding="utf-8"))])
    sys.path.insert(0, str(ROOT))
    from ce_config.loader import load_config
    from ce_contracts.plugins import discover

    bundle = load_config(ROOT / "config", "dev")
    cache = Path(os.environ.get("MODEL_CACHE_DIR") or ROOT / ".cache" / "models")
    cache = cache if cache.is_absolute() else ROOT / cache
    scratch = ROOT / ".data" / "calibration"
    scratch.mkdir(parents=True, exist_ok=True)
    registry = discover(app_env="dev", include_mocks=False)
    policy = bundle.qc_behavior
    cfg = policy.calibration.model_dump() if policy is not None else {}
    judge = bundle.app.behavior.judge
    rows: list[dict[str, Any]] = []
    for part in (
        visual(registry, bundle.vocab, judge, cfg, cache, args.fixtures, scratch),
        speech(registry, bundle.vocab, cfg, cache, scratch),
    ):
        found, message = await part
        print(message)
        rows += found
    for row in rows:
        data = row["measured"]
        print(
            f"  {row['adapter_id']}@{row['revision'][:12]} {row['proxy']}: "
            f"F1 {data['f1']:.2f} (n={data['n']}) → {data['reliability']}"
        )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1, sort_keys=True), encoding="utf-8")
    if rows and not args.dry_run:
        return await _store(rows)
    return 0


async def _store(rows: list[dict[str, Any]]) -> int:
    from ce_db.calibration import store_proxy_calibrations
    from ce_db.session import Database

    db = Database(os.environ["DATABASE_URL"], pool_size=1)
    async with db.transaction() as session:
        count = await store_proxy_calibrations(session, rows)
    await db.dispose()
    print(f"stored {count} calibration rows")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
