"""Knob calibration runs (§15.7, `CalibrationWorkflow`; Phase 8).

For every abstract knob an avatar engine declares, the engine renders the calibration fixture (a
portrait keyframe and a speech clip, `eval/smoke/`) at evenly spaced knob values — the knob is
set directly on the directives' sub-span, as the compiler would once calibrated — the face
analyzer measures the knob's effect on each clip (`ce_behavior.knobs.KNOB_EFFECTS`), and the
curve is fitted and stored in `model_behavior_profiles` (`dimension = 'knob:<name>'`). The
router overlay then marks the knob calibrated, and only then does the compiler use it.

Runs where the adapter can run: in the orchestrator for `cpu_model` engines (the mocks, CPU
engines), and on a GPU host through `scripts/calibrate/<plugin>.py` for GPU families, which
records the same report through the admin API. Dispatching calibration renders to the GPU fleet
is Phase 9 (fleet and providers)."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ce_behavior.knobs import KNOB_EFFECTS, fit_knob, sweep
from ce_contracts import models as m
from ce_contracts.behavior import BehaviorDirectives, DirectiveSubSpan, VisualDirectives
from ce_contracts.common import LoadContext, RunContext
from ce_contracts.local import LocalRunContext
from ce_contracts.plugins import LoadedPlugin, PluginRegistry

__all__ = [
    "CalibrationFixture",
    "calibrate_knobs",
    "default_fixture",
    "knob_directives",
    "pick_face_analyzer",
    "run_local_calibration",
]

ROOT = Path(__file__).resolve().parents[5]
FIXTURE_DIR = ROOT / "eval" / "smoke" / "media"


@dataclass(frozen=True)
class CalibrationFixture:
    keyframe: Path
    audio: Path
    width: int = 360
    height: int = 640
    fps: float = 25.0


def default_fixture(directory: Path | None = None) -> CalibrationFixture:
    """`eval/smoke/media/portrait_keyframe.png` and `speech_en_4s.wav` (synthetic: they exercise
    the pipeline, not a real face; real-engine calibration takes the owner's licensed fixtures via
    `--fixtures`, see eval/smoke/README.md)."""
    base = directory or FIXTURE_DIR
    return CalibrationFixture(keyframe=base / "portrait_keyframe.png", audio=base / "speech_en_4s.wav")


def knob_directives(
    knob: str, value: float, duration_s: float, character_key: str = "calibration"
) -> BehaviorDirectives:
    """One sub-span over the whole clip carrying only the knob (no labelled behavior to execute)."""
    return BehaviorDirectives(
        cbs_content_digest="sha256:" + "c" * 64,
        target_key="calibration:c1",
        realizations=[],
        visual=[
            VisualDirectives(
                shot_key="calibration",
                character_key=character_key,
                sub_spans=[DirectiveSubSpan(start_s=0.0, end_s=duration_s, item_refs=[], knobs={knob: value})],
            )
        ],
    )


def _duration(path: Path) -> float:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("ffprobe is required")
    out = subprocess.run(  # noqa: S603 - fixed binary
        [binary, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, check=True, timeout=60,
    ).stdout  # fmt: skip
    return float(json.loads(out)["format"]["duration"])


ContextFactory = Callable[[int], Awaitable[RunContext]]


async def calibrate_knobs(
    adapter: Any,
    manifest: Any,
    analyzer: Any,
    *,
    new_context: ContextFactory,
    fixture: CalibrationFixture,
    points: int = 5,
    seeds: Sequence[int] = (11, 12),
    knobs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """The calibration report of one avatar adapter: a fitted curve per knob it declares."""
    translator = None
    if manifest.behavior_translator:
        from ce_contracts.plugins import import_object

        translator = import_object(manifest.behavior_translator)()
    duration = _duration(fixture.audio)
    report: dict[str, Any] = {
        "adapter_id": manifest.id,
        "translator_version": getattr(translator, "version", "none"),
        "revision": manifest.models[0].source.revision if manifest.models else manifest.version,
        "analyzer": getattr(getattr(analyzer, "manifest", None), "id", "unknown"),
        "points": points,
        "seeds": list(seeds),
        "knobs": {},
    }
    for name in sorted(knobs or manifest.knobs):
        spec = manifest.knobs[name]
        effect = KNOB_EFFECTS.get(name)
        if effect is None:
            report["knobs"][name] = {"knob": name, "calibrated": False, "reasons": ["no effect metric for this knob"]}
            continue
        measured: list[tuple[float, float]] = []
        for value in sweep(points):
            values = []
            for seed in seeds:
                ctx = await new_context(seed)
                keyframe = await ctx.write_artifact(fixture.keyframe, "image", role="keyframe", mime="image/png")
                audio = await ctx.write_artifact(fixture.audio, "audio", role="audio", mime="audio/wav")
                request = m.AvatarRequest(
                    keyframe=keyframe, audio=audio, behavior=knob_directives(name, value, duration),
                    width=fixture.width, height=fixture.height, fps=fixture.fps, labels={"calibration": name},
                )  # fmt: skip
                result = await adapter.run("avatar.a2v", request, ctx)
                sidecars = [result.behavior_track] if getattr(result, "behavior_track", None) else []
                landmarks = await analyzer.run(
                    "face.landmarks", m.MediaAnalysisRequest(media=result.video, sample_hz=10.0, sidecars=sidecars), ctx
                )
                values.append(float(effect.measure(landmarks.series, landmarks.sample_hz)))
            measured.append((value, sum(values) / len(values)))
        report["knobs"][name] = fit_knob(measured, knob=name, param=spec.param, param_range=tuple(spec.range))
    return report


def pick_face_analyzer(registry: PluginRegistry, *, prefer_mock: bool) -> LoadedPlugin:
    """The `face.landmarks` analyzer to measure with: the mock observer for mock engines (it reads
    their ground-truth sidecars), else a real one."""
    found = registry.by_capability("face.landmarks")
    ordered = sorted(found, key=lambda p: (bool(p.manifest.mock) != prefer_mock, p.id))
    if not ordered:
        raise LookupError("no face.landmarks analyzer is installed")
    return ordered[0]


async def run_local_calibration(
    registry: PluginRegistry,
    adapter_id: str,
    work: Path,
    *,
    model_cache_dir: str,
    app_env: str,
    fixture: CalibrationFixture | None = None,
    points: int = 5,
    seeds: Sequence[int] = (11, 12),
    load_config: dict[str, Any] | None = None,
    backend_factory: str | None = None,
) -> dict[str, Any]:
    """Loads the adapter and the analyzer in this process and runs `calibrate_knobs`.
    `backend_factory` (a manifest `test_backend`) swaps in the CPU stand-in: a dry run of the
    calibration code, never a calibration of the model (scripts/calibrate refuses to record it)."""
    plugin = registry.get(adapter_id)
    if plugin.manifest.capability("avatar.a2v") is None or not plugin.manifest.knobs:
        raise ValueError(f"{adapter_id} declares no avatar knobs to calibrate")
    analyzer_plugin = pick_face_analyzer(
        registry, prefer_mock=bool(plugin.manifest.mock) or backend_factory is not None
    )
    load = LoadContext(
        model_cache_dir=model_cache_dir, scratch_dir=str(work / "scratch"), app_env=app_env, config=load_config or {}
    )
    adapter = plugin.adapter()
    if backend_factory is not None:
        from ce_contracts.plugins import import_object

        adapter.use_backend(import_object(backend_factory)(plugin.manifest))
    analyzer = analyzer_plugin.adapter()
    await adapter.load(load)
    await analyzer.load(load)
    counter = {"n": 0}

    async def new_context(seed: int) -> RunContext:
        counter["n"] += 1
        return LocalRunContext(work / f"run{counter['n']:03d}", seed=seed)

    return await calibrate_knobs(
        adapter, plugin.manifest, analyzer, new_context=new_context, fixture=fixture or default_fixture(),
        points=points, seeds=seeds,
    )  # fmt: skip


# ---------------------------------------------------------------------- the calibration job


async def store_report(session: Any, report: dict[str, Any], *, model_id: Any = None) -> int:
    """Stores every fitted curve of a report (`ce_db.registry.store_knob_calibration`)."""
    from ce_db.registry import store_knob_calibration

    stored = 0
    for name, curve in sorted(report.get("knobs", {}).items()):
        if "points" not in curve:
            continue
        await store_knob_calibration(
            session,
            adapter_id=str(report["adapter_id"]),
            translator_version=str(report["translator_version"]),
            revision=str(report["revision"]),
            knob=name,
            curve={**curve, "analyzer": report.get("analyzer"), "seeds": report.get("seeds")},
            model_id=model_id,
        )
        stored += 1
    return stored


async def calibrate_model_job(svc: Any, org_id: Any, job_id: Any, model_id: Any) -> dict[str, Any]:
    """`CalibrationWorkflow`'s activity: calibrates the model's adapter here when it runs on CPU
    (`cpu_model`: mocks and CPU engines); a GPU-family adapter fails the job with
    `needs_gpu_host` and the command to run on a host of its family (no GPU is claimed)."""
    import tempfile

    import sqlalchemy as sa
    from ce_db import execution as rec
    from ce_db.models.platform import Model, Plugin
    from ce_obs.events import EventType

    from ce_exec.jobs import job_event

    async def set_job(extra: dict[str, Any] | None = None, **values: Any) -> None:
        async with svc.db.transaction() as session:
            job = await rec.set_job(session, org_id, job_id, **values)
            event = {**job_event(job), **(extra or {})}
        await svc.publish(org_id, EventType.JOB_UPDATED, event)

    await set_job(status="running", progress=0.05)
    async with svc.db.session() as session:
        row = await session.get(Model, model_id)
        plugin_key = (
            (await session.execute(sa.select(Plugin.plugin_key).where(Plugin.id == row.plugin_id))).scalar()
            if row is not None
            else None
        )
    if plugin_key is None or plugin_key not in svc.registry.plugins:
        await set_job(
            status="failed", error={"code": "not_installed", "message": "the model's plugin is not installed here"}
        )
        return {"status": "failed", "code": "not_installed"}
    manifest = svc.registry.get(plugin_key).manifest
    if manifest.runtime.family != "cpu_model":
        message = (
            f"{plugin_key} runs on the {manifest.runtime.family} GPU family: run "
            f"`python scripts/calibrate/run.py {plugin_key} --record` on a host of that family"
        )
        await set_job(status="failed", error={"code": "needs_gpu_host", "message": message})
        return {"status": "failed", "code": "needs_gpu_host", "message": message}
    try:
        with tempfile.TemporaryDirectory(prefix="ce-calibrate-") as tmp:
            report = await run_local_calibration(
                svc.registry,
                plugin_key,
                Path(tmp),
                model_cache_dir=svc.settings.model_cache_dir,
                app_env=svc.settings.app_env,
            )
    except Exception as exc:
        await set_job(status="failed", error={"code": "calibration_failed", "message": str(exc)[:500]})
        raise
    async with svc.db.transaction() as session:
        stored = await store_report(session, report, model_id=model_id)
    await svc.reload_catalog()
    summary = {k: {"calibrated": v.get("calibrated"), "rho": v.get("rho")} for k, v in report["knobs"].items()}
    await set_job({"result": {"knobs": summary}}, status="succeeded", progress=1.0)
    return {"status": "succeeded", "stored": stored, "knobs": summary}
