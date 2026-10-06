"""Smoke and bench runs of one adapter on the smoke golden set (§24 sandbox and promotion, Phase 8).

`run_validation(plugin, smoke_set, mode=...)` loads the adapter, runs every case of
`eval/smoke/cases.yaml` whose capability it declares (and whose language it supports), checks the
outputs and returns a JSON-able report:

- `mode="smoke"`: one seed per case; the verdict is `smoke_passed` only when every applicable case
  passed and every declared capability had a case (`provenance.verify` is covered by the watermark
  round trips);
- `mode="bench"`: every case over several seeds; timings (p50/p90, seconds per work unit), load
  time, peak VRAM and the behavior measurements. Its verdict stays `pending`: bench-based
  promotion needs the full evaluation set and human ratings (Phase 11).

**Rule 5.** With `backend="test"` the adapter runs on its CPU stand-in (manifest `test_backend`):
that exercises the harness and the adapter's own code, not the model. Such a report says
`backend: test` and its verdict is `stand_in_only`; the recording helpers and the API refuse it.
Only `backend="real"` on a host with the weights can produce `smoke_passed`.

Behavior measurements (`measure`) come from analyzers installed in the same environment
(`face.landmarks` for face presence and head motion, `audio.prosody` for pauses). They are recorded,
never pass/fail: measured behavior feeds profiles (ADR 0027), it does not gate a smoke run.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import platform
import re
import shutil
import statistics
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from ce_contracts.capabilities import CAPABILITIES
from ce_contracts.common import ArtifactRef, LoadContext, RunContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import LoadedPlugin, PluginRegistry, import_object

__all__ = [
    "CaseResult",
    "SmokeCase",
    "SmokeSet",
    "StandInReportError",
    "applicable_cases",
    "assert_recordable",
    "character_error_rate",
    "load_smoke_set",
    "run_validation",
    "word_error_rate",
]

COVERED_BY_ROUNDTRIP = frozenset({"provenance.verify"})


class StandInReportError(ValueError):
    """A report produced on a CPU stand-in (or edited to look real) cannot become evidence."""


# ---------------------------------------------------------------------- the golden set


@dataclass(frozen=True)
class SmokeCase:
    id: str
    capability: str
    request: dict[str, Any]
    checks: dict[str, Any] = field(default_factory=dict)
    measure: tuple[str, ...] = ()
    language: str | None = None


@dataclass(frozen=True)
class SmokeSet:
    version: str
    root: Path
    media: dict[str, dict[str, Any]]
    reference_text: dict[str, str]
    cases: tuple[SmokeCase, ...]
    pairwise: tuple[str, ...] = ()  # capabilities whose outputs are rated in blind pairs (eval set)

    def media_path(self, name: str) -> Path:
        return self.root / str(self.media[name]["path"])

    def verify_media(self) -> list[str]:
        """Media files whose sha256 differs from the pin (empty = all verified)."""
        bad = []
        for name, entry in sorted(self.media.items()):
            path = self.media_path(name)
            if not path.is_file() or _sha256(path) != entry.get("sha256"):
                bad.append(name)
        return bad


def load_smoke_set(path: Path | str) -> SmokeSet:
    """The smoke set, or the evaluation set (`eval/cases.yaml`), which `extends` the smoke set: its
    media, reference texts and cases come first (paths stay relative to the smoke set's folder)."""
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    base: SmokeSet | None = None
    if data.get("extends"):
        base = load_smoke_set(path.parent / str(data["extends"]))
    cases = list(base.cases) if base else []
    seen: set[str] = {c.id for c in cases}
    for raw in data.get("cases", []):
        if raw["id"] in seen:
            raise ValueError(f"duplicate smoke case id {raw['id']}")
        seen.add(raw["id"])
        if raw["capability"] not in CAPABILITIES:
            raise ValueError(f"{raw['id']}: unknown capability {raw['capability']}")
        cases.append(
            SmokeCase(
                id=raw["id"],
                capability=raw["capability"],
                request=dict(raw.get("request", {})),
                checks=dict(raw.get("checks", {})),
                measure=tuple(raw.get("measure", ())),
                language=raw.get("language"),
            )
        )
    if base is not None:
        return SmokeSet(
            version=str(data["version"]),
            root=base.root,
            media={**base.media, **dict(data.get("media", {}))},
            reference_text={**base.reference_text, **dict(data.get("reference_text", {}))},
            cases=tuple(cases),
            pairwise=tuple(data.get("pairwise", ())),
        )
    return SmokeSet(
        version=str(data["version"]),
        root=path.parent,
        media=dict(data.get("media", {})),
        reference_text=dict(data.get("reference_text", {})),
        cases=tuple(cases),
        pairwise=tuple(data.get("pairwise", ())),
    )


def _primary(language: str) -> str:
    return language.split("-")[0].lower()


def applicable_cases(manifest: Any, smoke_set: SmokeSet) -> tuple[list[SmokeCase], list[str], list[str]]:
    """(cases to run, skipped case ids with the reason, declared capabilities without a case)."""
    run: list[SmokeCase] = []
    skipped: list[str] = []
    for case in smoke_set.cases:
        decl = manifest.capability(case.capability)
        if decl is None:
            continue
        if case.language and (decl.languages.validated or decl.languages.unvalidated):
            supported = {_primary(x) for x in [*decl.languages.validated, *decl.languages.unvalidated]}
            if _primary(case.language) not in supported:
                skipped.append(f"{case.id}: language {case.language} not declared")
                continue
        run.append(case)
    covered = {c.capability for c in run}
    missing = [c.id for c in manifest.capabilities if c.id not in covered and c.id not in COVERED_BY_ROUNDTRIP]
    return run, skipped, missing


# ---------------------------------------------------------------------- media probes


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _binary(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"{name} is required by the validation harness")
    return found


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603 - fixed binary
        [_binary("ffprobe"), "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,width,height,r_frame_rate", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=120, text=True,
    ).stdout  # fmt: skip
    data = json.loads(out)
    info: dict[str, Any] = {"duration_s": float(data.get("format", {}).get("duration") or 0.0)}
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and "width" not in info:
            num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
            info.update(
                width=int(stream.get("width", 0)),
                height=int(stream.get("height", 0)),
                fps=float(num) / float(den or 1) if float(den or 1) else 0.0,
            )
        if stream.get("codec_type") == "audio":
            info["has_audio"] = True
    return info


def mean_volume_db(path: Path) -> float:
    err = subprocess.run(  # noqa: S603
        [_binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, check=True, timeout=300, text=True,
    ).stderr  # fmt: skip
    found = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", err)
    if not found or found.group(1) == "-inf":
        return -math.inf
    return float(found.group(1))


def luma_range(path: Path) -> float:
    err = subprocess.run(  # noqa: S603
        [_binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-vf", "signalstats,metadata=print",
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, check=True, timeout=120, text=True,
    ).stderr  # fmt: skip
    low = re.search(r"lavfi\.signalstats\.YMIN=([\d.]+)", err)
    high = re.search(r"lavfi\.signalstats\.YMAX=([\d.]+)", err)
    return float(high.group(1)) - float(low.group(1)) if low and high else 0.0


def silences(path: Path, *, min_s: float = 0.2, noise_db: float = -40.0) -> list[tuple[float, float]]:
    err = subprocess.run(  # noqa: S603
        [_binary("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af",
         f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"],
        capture_output=True, check=True, timeout=300, text=True,
    ).stderr  # fmt: skip
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    return list(zip(starts, ends, strict=False))


_WORD = re.compile(r"[\w']+", re.UNICODE)


def _edit_distance(ref: Sequence[str], hyp: Sequence[str]) -> int:
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[-1]


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Character-level edit distance over the letters and digits only (case-insensitive)."""
    ref = "".join(_WORD.findall(reference.lower()))
    hyp = "".join(_WORD.findall(hypothesis.lower()))
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance / reference length, case- and punctuation-insensitive."""
    ref = _WORD.findall(reference.lower())
    hyp = _WORD.findall(hypothesis.lower())
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


# ---------------------------------------------------------------------- requests


def _resolve(value: Any, refs: Mapping[str, ArtifactRef], smoke_set: SmokeSet) -> Any:
    if isinstance(value, str):
        if value.startswith("$ref:"):
            return smoke_set.reference_text[value[5:]]
        if value.startswith("$"):
            return refs[value[1:]]
        if value.startswith("@"):
            return json.loads((smoke_set.root / value[1:]).read_text(encoding="utf-8"))
        return value
    if isinstance(value, list):
        return [_resolve(v, refs, smoke_set) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, refs, smoke_set) for k, v in value.items()}
    return value


def build_request(case: SmokeCase, refs: Mapping[str, ArtifactRef], smoke_set: SmokeSet, plugin: LoadedPlugin) -> Any:
    """The typed contract request, translated by the adapter's own translator when it carries
    behavior directives (as the worker runtime does)."""
    spec = CAPABILITIES[case.capability]
    data = _resolve(case.request, refs, smoke_set)
    if "text" in data and "words" in spec.request.model_fields and "words" not in data:
        data["words"] = str(data["text"]).split()
    request = spec.request.model_validate(data)
    behavior = getattr(request, "behavior", None)
    if behavior is not None and not getattr(request, "engine", None):
        translator = plugin.translator()
        if translator is not None:
            request = translator.translate(behavior, request)
    return request


# ---------------------------------------------------------------------- checks


@dataclass
class Check:
    name: str
    passed: bool
    value: Any = None
    expected: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "value": self.value, "expected": self.expected}


def _field(result: Any, name: str) -> Any:
    value = result
    for part in name.split("."):
        index = None
        if part.endswith("]"):
            part, _, rest = part.partition("[")
            index = int(rest[:-1])
        value = getattr(value, part, None)
        if index is not None and value is not None:
            value = value[index]
    return value


def _range(name: str, value: float, spec: Mapping[str, Any], low: str, high: str) -> list[Check]:
    out = []
    if low in spec:
        out.append(Check(f"{name}>={spec[low]}", value >= float(spec[low]), round(value, 4), spec[low]))
    if high in spec:
        out.append(Check(f"{name}<={spec[high]}", value <= float(spec[high]), round(value, 4), spec[high]))
    return out


async def _media_checks(kind: str, spec: Mapping[str, Any], result: Any, ctx: RunContext) -> list[Check]:
    ref = _field(result, str(spec.get("field", kind)))
    if not isinstance(ref, ArtifactRef):
        return [Check(f"{kind}.present", False, None, "an artifact")]
    path = await ctx.read_artifact(ref)
    info = await asyncio.to_thread(probe, path)
    checks = [Check(f"{kind}.present", path.stat().st_size > 0, path.stat().st_size, ">0 bytes")]
    for key in ("width", "height"):
        if key in spec:
            checks.append(Check(f"{kind}.{key}", info.get(key) == int(spec[key]), info.get(key), spec[key]))
    if kind != "image":
        checks += _range(f"{kind}.duration_s", info["duration_s"], spec, "min_duration_s", "max_duration_s")
    if "min_fps" in spec or "max_fps" in spec:
        checks += _range(f"{kind}.fps", float(info.get("fps", 0.0)), spec, "min_fps", "max_fps")
    if "min_mean_volume_db" in spec:
        volume = await asyncio.to_thread(mean_volume_db, path)
        checks.append(
            Check(
                "audio.mean_volume_db", volume >= float(spec["min_mean_volume_db"]), volume, spec["min_mean_volume_db"]
            )
        )
    if "min_luma_range" in spec:
        spread = await asyncio.to_thread(luma_range, path)
        checks.append(
            Check("image.luma_range", spread >= float(spec["min_luma_range"]), spread, spec["min_luma_range"])
        )
    return checks


RoundTrip = Callable[[str, Any], Awaitable[Any]]


async def run_checks(
    case: SmokeCase, result: Any, ctx: RunContext, smoke_set: SmokeSet, verify: RoundTrip
) -> list[Check]:
    checks: list[Check] = []
    spec_type = CAPABILITIES[case.capability].result
    checks.append(Check("result_type", isinstance(result, spec_type), type(result).__name__, spec_type.__name__))
    for name, raw in case.checks.items():
        spec = _resolve(raw, {}, smoke_set) if isinstance(raw, dict) else raw
        if name in ("video", "audio", "image"):
            checks += await _media_checks(name, spec, result, ctx)
        elif name == "artifact":
            ref = _field(result, str(spec["field"]))
            ok = isinstance(ref, ArtifactRef) and (await ctx.read_artifact(ref)).stat().st_size > 0
            checks.append(Check(f"artifact.{spec['field']}", ok, None, "a non-empty artifact"))
        elif name == "candidates":
            candidates = list(getattr(result, "candidates", []) or [])
            checks.append(
                Check("candidates.count", len(candidates) == int(spec["count"]), len(candidates), spec["count"])
            )
            for i, ref in enumerate(candidates):
                info = await asyncio.to_thread(probe, await ctx.read_artifact(ref))
                checks.append(
                    Check(f"candidates[{i}].duration_s", info["duration_s"] > 0.5, info["duration_s"], ">0.5")
                )
        elif name == "transcript":
            text = str(getattr(result, "text", ""))
            if "max_wer" in spec:
                wer = word_error_rate(str(spec["reference"]), text)
                checks.append(
                    Check("transcript.wer", wer <= float(spec["max_wer"]), {"wer": round(wer, 4), "text": text[:300]},
                          spec["max_wer"])
                )  # fmt: skip
            if "max_cer" in spec:
                cer = character_error_rate(str(spec["reference"]), text)
                checks.append(
                    Check("transcript.cer", cer <= float(spec["max_cer"]), {"cer": round(cer, 4), "text": text[:300]},
                          spec["max_cer"])
                )  # fmt: skip
        elif name == "language":
            got = _primary(str(getattr(result, "language", "")))
            checks.append(Check("language", got == spec["expected"], got, spec["expected"]))
        elif name == "alignment":
            words = list(getattr(result, "words", []) or [])
            starts = [w.start_s for w in words]
            checks.append(Check("alignment.words", len(words) > 0, len(words), ">0"))
            if spec.get("monotonic"):
                ok = all(a <= b for a, b in itertools.pairwise(starts))
                ok = ok and all(w.end_s >= w.start_s for w in words)
                checks.append(Check("alignment.monotonic", ok, None, True))
            if "within_s" in spec and words:
                last = max(w.end_s for w in words)
                checks.append(Check("alignment.within_s", last <= float(spec["within_s"]), last, spec["within_s"]))
        elif name == "answer":
            answer = dict(getattr(result, "answer", {}) or {})
            missing = [k for k in spec.get("keys", []) if k not in answer]
            checks.append(Check("answer.keys", not missing, sorted(answer), spec.get("keys")))
        elif name == "classes":
            classes = dict(getattr(result, "classes", {}) or {})
            total = sum(float(v) for v in classes.values())
            ok = len(classes) >= int(spec.get("min", 1)) and abs(total - 1.0) < 0.05
            checks.append(Check("classes.distribution", ok, {"n": len(classes), "sum": round(total, 4)}, spec))
        elif name == "score":
            score = float(getattr(result, "score", math.nan))
            checks.append(Check("score.finite", math.isfinite(score), score, "finite"))
        elif name == "vectors":
            dim = int(getattr(result, "dim", 0) or 0)
            vectors = list(getattr(result, "vectors", []) or [])
            ok = bool(vectors) and dim >= int(spec["min_dim"]) and all(len(v) == dim for v in vectors)
            checks.append(Check("vectors.dim", ok, dim, f">={spec['min_dim']}"))
        elif name == "watermark_roundtrip":
            media = getattr(result, "media", None)
            payload = str(case.request.get("payload_id", ""))
            verdict = await verify(str(spec["layer"]), {"media": media, "payload_id": payload})
            present = bool(getattr(verdict, "present", False)) if verdict is not None else False
            checks.append(Check("watermark.detected", present, getattr(verdict, "detail", None), True))
        else:
            checks.append(Check(f"unknown_check:{name}", False, None, "a known check"))
    return checks


# ---------------------------------------------------------------------- measurements


def _head_motion(series: Mapping[str, Sequence[float]], sample_hz: float) -> float:
    """Mean absolute head rotation speed (deg/s) from yaw and pitch series — the same metric as
    `ce_behavior.knobs.head_motion_energy`, without NumPy (GPU images run Python 3.10)."""
    yaw, pitch = list(series.get("head_yaw_deg", [])), list(series.get("head_pitch_deg", []))
    n = min(len(yaw), len(pitch))
    if n < 2:
        return 0.0
    steps = [abs(yaw[i + 1] - yaw[i]) + abs(pitch[i + 1] - pitch[i]) for i in range(n - 1)]
    return statistics.fmean(steps) * sample_hz


async def measure(
    kinds: Sequence[str], case: SmokeCase, request: Any, result: Any, ctx: RunContext, analyzers: Mapping[str, Any]
) -> dict[str, Any]:
    """Behavior observed in the output (recorded only). Missing analyzers are reported as such."""
    out: dict[str, Any] = {}
    from ce_contracts import models as m

    for kind in kinds:
        if kind in ("face", "head_motion"):
            analyzer = analyzers.get("face.landmarks")
            video = getattr(result, "video", None)
            if analyzer is None or video is None:
                out[kind] = {"measured": False, "reason": "no face.landmarks analyzer installed"}
                continue
            sidecars = [result.behavior_track] if getattr(result, "behavior_track", None) else []
            landmarks = await analyzer.run(
                "face.landmarks", m.MediaAnalysisRequest(media=video, sample_hz=10.0, sidecars=sidecars), ctx
            )
            if kind == "face":
                out[kind] = {"measured": True, "face_detected_ratio": landmarks.face_detected_ratio}
            else:
                out[kind] = {
                    "measured": True,
                    "head_motion_deg_per_s": round(_head_motion(landmarks.series, landmarks.sample_hz), 4),
                }
        elif kind == "pauses":
            audio = getattr(result, "audio", None)
            if audio is None:
                out[kind] = {"measured": False, "reason": "no audio"}
                continue
            found = await asyncio.to_thread(silences, await ctx.read_artifact(audio))
            requested = []
            for prosody in getattr(getattr(request, "behavior", None), "prosody", None) or []:
                requested += [int(p.get("ms", 0)) for p in prosody.pauses]
            inner = [(round(a, 3), round(b, 3)) for a, b in found if a > 0.05]
            out[kind] = {
                "measured": True,
                "requested_ms": requested,
                "silences_s": inner,
                "longest_ms": round(max((b - a for a, b in inner), default=0.0) * 1000),
            }
        else:
            out[kind] = {"measured": False, "reason": f"unknown measurement {kind}"}
    return out


# ---------------------------------------------------------------------- the run


@dataclass
class CaseResult:
    case_id: str
    capability: str
    seed: int
    passed: bool
    seconds: float
    work_units: float
    checks: list[dict[str, Any]] = field(default_factory=list)
    measurements: dict[str, Any] = field(default_factory=dict)
    peak_vram_gb: float | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _git_commit(root: Path) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        return subprocess.run(  # noqa: S603
            [git, "-C", str(root), "rev-parse", "HEAD"], capture_output=True, check=True, text=True, timeout=10
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def _units(request: Any) -> float:
    try:
        from ce_plugin_kit.engine import work_units

        return float(work_units(request))
    except ImportError:  # pragma: no cover - the plugin kit ships with every engine
        return 1.0


def _pct(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo), 4)


async def run_validation(
    plugin: LoadedPlugin,
    smoke_set: SmokeSet,
    *,
    mode: str = "smoke",
    backend: str = "real",
    work: Path,
    model_cache_dir: Path | str,
    seeds: Sequence[int] = (1234,),
    analyzers: Mapping[str, Any] | None = None,
    model_paths: Mapping[str, str] | None = None,
    load_config: Mapping[str, Any] | None = None,
    verifier: LoadedPlugin | None = None,
    case_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Runs the applicable cases and returns the report (see the module docstring)."""
    if mode not in ("smoke", "bench"):
        raise ValueError(f"mode is smoke or bench, not {mode}")
    if backend not in ("real", "test"):
        raise ValueError(f"backend is real or test, not {backend}")
    manifest = plugin.manifest
    started_at = datetime.now(timezone.utc).isoformat()
    bad_media = smoke_set.verify_media()
    if bad_media:
        raise ValueError(f"smoke media do not match their sha256 pins: {bad_media} (scripts/gen_smoke_media.py)")
    cases, skipped, missing = applicable_cases(manifest, smoke_set)
    if case_ids:
        cases = [c for c in cases if c.id in set(case_ids)]
    adapter = plugin.adapter()
    if backend == "test":
        if not manifest.test_backend:
            raise ValueError(f"{manifest.id} has no test_backend")
        adapter.use_backend(import_object(str(manifest.test_backend))(manifest))
    work.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = dict(load_config or {})
    if model_paths:
        config["model_paths"] = dict(model_paths)
    from ce_plugin_kit import gpu

    gpu.reset_peak_vram()
    load_started = time.monotonic()
    await adapter.load(
        LoadContext(
            model_cache_dir=str(model_cache_dir), scratch_dir=str(work / "scratch"), app_env="test", config=config
        )
    )
    load_seconds = round(time.monotonic() - load_started, 3)
    verify_adapter = adapter if manifest.capability("provenance.verify") else None
    if verify_adapter is None and verifier is not None:
        verify_adapter = verifier.adapter()
        await verify_adapter.load(
            LoadContext(model_cache_dir=str(model_cache_dir), scratch_dir=str(work / "scratch"), app_env="test")
        )
    results: list[CaseResult] = []
    for case in cases:
        for seed in seeds if mode == "bench" else seeds[:1]:
            ctx = LocalRunContext(work / f"{case.id}-{seed}", seed=seed)
            refs = {name: await ctx.put_file(smoke_set.media_path(name), entry["kind"]) for name, entry in
                    smoke_set.media.items()}  # fmt: skip

            async def verify(layer: str, data: Any, ctx: LocalRunContext = ctx) -> Any:
                if verify_adapter is None or data.get("media") is None:
                    return None
                request = CAPABILITIES["provenance.verify"].request.model_validate({"layer": layer, **data})
                return await verify_adapter.run("provenance.verify", request, ctx)

            gpu.reset_peak_vram()
            t0 = time.monotonic()
            try:
                request = build_request(case, refs, smoke_set, plugin)
                units = _units(request)
                result = await adapter.run(case.capability, request, ctx)
                seconds = round(time.monotonic() - t0, 3)
                checks = await run_checks(case, result, ctx, smoke_set, verify)
                try:
                    measured = await measure(case.measure, case, request, result, ctx, analyzers or {})
                except Exception as exc:  # measurements are recorded, never pass/fail
                    measured = {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}
                results.append(
                    CaseResult(
                        case.id, case.capability, seed, all(c.passed for c in checks), seconds, units,
                        [c.as_dict() for c in checks], measured, gpu.peak_vram_gb(),
                    )
                )  # fmt: skip
            except Exception as exc:  # a crash is a failed case, with the reason
                results.append(
                    CaseResult(case.id, case.capability, seed, False, round(time.monotonic() - t0, 3), 0.0,
                               error=f"{type(exc).__name__}: {str(exc)[:500]}")
                )  # fmt: skip
    await adapter.unload()
    passed = bool(cases) and all(r.passed for r in results) and not missing
    report: dict[str, Any] = {
        "kind": mode,
        "eval_set_version": smoke_set.version,
        "adapter_id": manifest.id,
        "adapter_version": manifest.version,
        "model_keys": [m.key for m in manifest.models],
        "revisions": {m.key: m.source.revision for m in manifest.models},
        "translator_version": getattr(plugin.translator(), "version", None),
        "backend": backend,
        "host": {"platform": platform.platform(), "python": platform.python_version(), **gpu.gpu_summary()},
        "git_commit": _git_commit(smoke_set.root),
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "load_seconds": load_seconds,
        "cases": [r.as_dict() for r in results],
        "skipped": skipped,
        "missing_capabilities": missing,
        "summary": {"cases": len(results), "passed": sum(r.passed for r in results)},
    }
    if mode == "bench":
        report["timings"] = _timings(results)
        report["verdict"] = "pending" if backend == "real" else "stand_in_only"
    else:
        report["verdict"] = ("smoke_passed" if passed else "failed") if backend == "real" else "stand_in_only"
    return report


def _timings(results: Sequence[CaseResult]) -> dict[str, Any]:
    by_case: dict[str, list[CaseResult]] = {}
    for r in results:
        if r.passed:
            by_case.setdefault(r.case_id, []).append(r)
    out: dict[str, Any] = {}
    for case_id, runs in sorted(by_case.items()):
        seconds = [r.seconds for r in runs]
        per_unit = [r.seconds / r.work_units for r in runs if r.work_units > 0]
        vram = [r.peak_vram_gb for r in runs if r.peak_vram_gb is not None]
        out[case_id] = {
            "n": len(runs),
            "p50_s": _pct(seconds, 0.5),
            "p90_s": _pct(seconds, 0.9),
            "seconds_per_unit_p50": _pct(per_unit, 0.5),
            "peak_vram_gb": max(vram) if vram else None,
        }
    all_per_unit = [r.seconds / r.work_units for r in results if r.passed and r.work_units > 0]
    out["_all"] = {"seconds_per_unit_p50": _pct(all_per_unit, 0.5), "seconds_per_unit_p90": _pct(all_per_unit, 0.9)}
    return out


def assert_recordable(report: Mapping[str, Any]) -> None:
    """Rule 5 at the recording boundary: only real-backend runs become evidence."""
    if report.get("backend") != "real":
        raise StandInReportError("a run on a CPU stand-in (backend: test) is never evidence of a model (rule 5)")
    if report.get("kind") == "smoke" and report.get("verdict") not in ("smoke_passed", "failed"):
        raise StandInReportError(f"a smoke report with verdict {report.get('verdict')!r} cannot be recorded")
    if report.get("kind") == "smoke":
        cases = list(report.get("cases", []))
        claims_pass = report.get("verdict") == "smoke_passed"
        if claims_pass and (not cases or not all(c.get("passed") for c in cases) or report.get("missing_capabilities")):
            raise StandInReportError("the report claims smoke_passed but not every case passed")


def analyzers_from(registry: PluginRegistry, *, prefer_mock: bool) -> dict[str, Any]:
    """Loaded analyzer adapters for the measurements (`face.landmarks`, `audio.prosody`)."""
    out: dict[str, Any] = {}
    for capability in ("face.landmarks", "audio.prosody"):
        found = sorted(registry.by_capability(capability), key=lambda p: (bool(p.manifest.mock) != prefer_mock, p.id))
        if found:
            out[capability] = found[0]
    return out
