"""Command line of the smoke, bench and calibration runs (`scripts/smoke/run.py`,
`scripts/bench/run.py`, `scripts/calibrate/run.py`; `make smoke-gpu|bench|calibrate PLUGIN=…`).

    python scripts/smoke/run.py <adapter> [--backend real|test] [--cases eval/smoke/cases.yaml]
        [--out report.json] [--fetch] [--record --api https://host --api-key ce_key_…]

- `--backend real` (default) loads the model weights from the model cache (`MODEL_CACHE_DIR`;
  `--fetch` downloads and verifies them first through `ce_worker.models`). It needs the adapter's
  runtime family installed (its worker image, `infra/docker/worker-<family>.Dockerfile`) and,
  for GPU families, a GPU.
- `--backend test` runs the adapter's CPU stand-in: a dry run of the harness and the adapter code.
  Its report says `stand_in_only` and `--record` refuses it (rule 5).
- `--record` posts the report to the registry API as a platform admin (`CE_API_KEY`): smoke → the
  model's validation evidence; bench → a `model_benchmarks` row (verdict `pending`);
  calibrate → the knob curves.

Exit status: 0 when the run produced its report and (for smoke) every case passed; 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from ce_contracts.common import LoadContext
from ce_contracts.plugins import PluginRegistry, discover

from ce_worker.validation import analyzers_from, assert_recordable, load_smoke_set, run_validation

__all__ = ["main"]

ROOT = Path(__file__).resolve().parents[5]
DEFAULT_CASES = ROOT / "eval" / "smoke" / "cases.yaml"


def _parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"scripts/{command}/run.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("adapter", help="adapter id (plugin manifest id), e.g. infinitetalk")
    parser.add_argument("--backend", choices=("real", "test"), default="real")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--case", action="append", default=[], help="run only these case ids")
    parser.add_argument("--out", type=Path, help="write the JSON report here (default: stdout)")
    parser.add_argument("--work", type=Path, help="keep outputs in this directory (default: a temporary one)")
    parser.add_argument("--model-cache", default=os.environ.get("MODEL_CACHE_DIR", str(ROOT / ".cache" / "models")))
    parser.add_argument("--fetch", action="store_true", help="download and verify the weights into the cache first")
    parser.add_argument("--record", action="store_true", help="post the report to the registry API")
    parser.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.environ.get("CE_API_KEY", ""))
    if command == "bench":
        parser.add_argument("--seeds", default="11,12,13", help="comma-separated seeds")
    if command == "calibrate":
        parser.add_argument("--points", type=int, default=5)
        parser.add_argument("--seeds", default="11,12", help="comma-separated seeds")
    return parser


def _registry() -> PluginRegistry:
    return discover(app_env=None, include_mocks=True)


async def _model_paths(manifest: Any, cache_dir: str, fetch: bool) -> dict[str, str]:
    if not fetch:
        return {}
    from ce_worker.models import ensure_plugin_models
    from ce_worker.runtime import default_cache

    return await ensure_plugin_models(default_cache(cache_dir), manifest)


async def _loaded_analyzers(
    registry: PluginRegistry, work: Path, cache_dir: str, *, prefer_mock: bool
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for capability, plugin in analyzers_from(registry, prefer_mock=prefer_mock).items():
        adapter = plugin.adapter()
        try:
            await adapter.load(
                LoadContext(model_cache_dir=cache_dir, scratch_dir=str(work / "analyzers"), app_env="test")
            )
        except Exception as exc:  # an analyzer without its assets measures nothing (reported per case)
            print(f"note: analyzer {plugin.id} not loaded ({type(exc).__name__}: {str(exc)[:120]})", file=sys.stderr)
            continue
        out[capability] = adapter
    return out


def _verifier(registry: PluginRegistry, adapter_id: str) -> Any:
    """For a watermarker without its own `provenance.verify`: a sibling adapter that reads the
    same layers (the round-trip check). None when the adapter verifies itself or watermarks nothing."""
    manifest = registry.get(adapter_id).manifest
    layers = {c.id for c in manifest.capabilities if c.id.startswith("provenance.watermark_")}
    if not layers or manifest.capability("provenance.verify") is not None:
        return None
    for plugin in registry.by_capability("provenance.verify"):
        theirs = {c.id for c in plugin.manifest.capabilities}
        if plugin.manifest.mock == manifest.mock and plugin.id != adapter_id and layers <= theirs:
            return plugin
    return None


async def _run(command: str, args: argparse.Namespace, work: Path) -> dict[str, Any]:
    registry = _registry()
    plugin = registry.get(args.adapter)
    if command == "calibrate":
        from ce_exec.calibration import run_local_calibration  # the workspace environment (ce_behavior)

        config: dict[str, Any] = {}
        if args.backend == "test" and not plugin.manifest.test_backend:
            raise SystemExit(f"{args.adapter} has no test_backend")
        if args.backend == "real":
            config["model_paths"] = await _model_paths(plugin.manifest, args.model_cache, args.fetch)
        report = await run_local_calibration(
            registry, args.adapter, work, model_cache_dir=args.model_cache, app_env="test", points=args.points,
            seeds=[int(s) for s in args.seeds.split(",")], load_config=config,
            backend_factory=plugin.manifest.test_backend if args.backend == "test" else None,
        )  # fmt: skip
        report["backend"] = args.backend
        report["kind"] = "calibrate"
        return report
    smoke_set = load_smoke_set(args.cases)
    paths = await _model_paths(plugin.manifest, args.model_cache, args.fetch) if args.backend == "real" else {}
    analyzers = await _loaded_analyzers(registry, work, args.model_cache, prefer_mock=bool(plugin.manifest.mock))
    seeds = [int(s) for s in args.seeds.split(",")] if command == "bench" else [1234]
    return await run_validation(
        plugin, smoke_set, mode=command, backend=args.backend, work=work, model_cache_dir=args.model_cache,
        seeds=seeds, analyzers=analyzers, model_paths=paths, verifier=_verifier(registry, args.adapter),
        case_ids=args.case or None,
    )  # fmt: skip


def record(command: str, report: dict[str, Any], *, api: str, api_key: str) -> list[dict[str, Any]]:
    """Posts the report for every registry model of the adapter (platform admin API key)."""
    import httpx

    if command != "calibrate":
        assert_recordable(report)
    elif report.get("backend") != "real":
        raise SystemExit("a calibration on a CPU stand-in is never recorded (rule 5)")
    if not api_key:
        raise SystemExit("--record needs --api-key or CE_API_KEY (a platform admin's API key)")
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=api, headers=headers, timeout=60) as client:
        models = client.get("/v1/models").raise_for_status().json()
        mine = [m for m in models if m.get("plugin_key") == report["adapter_id"]]
        if not mine:
            raise SystemExit(f"no registry model belongs to {report['adapter_id']} (has the orchestrator synced it?)")
        out = []
        for model in mine:
            if command == "smoke":
                body = {"validation": report["verdict"], "report": report}
                response = client.post(f"/v1/admin/models/{model['id']}/validations", json=body)
            elif command == "bench":
                response = client.post(f"/v1/admin/models/{model['id']}/benchmarks", json={"report": report})
            else:
                response = client.post(f"/v1/admin/models/{model['id']}/calibrations", json={"report": report})
                out.append(response.raise_for_status().json())
                break  # knob curves belong to the adapter, not to each of its models
            out.append(response.raise_for_status().json())
        return out


def main(command: str, argv: list[str] | None = None) -> int:
    args = _parser(command).parse_args(argv)
    if args.work:
        args.work.mkdir(parents=True, exist_ok=True)
        report = asyncio.run(_run(command, args, args.work))
    else:
        with tempfile.TemporaryDirectory(prefix=f"ce-{command}-") as tmp:
            report = asyncio.run(_run(command, args, Path(tmp)))
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    if args.record:
        recorded = record(command, report, api=args.api, api_key=args.api_key)
        print(f"recorded for {len(recorded)} model(s)", file=sys.stderr)
    verdict = report.get("verdict")
    print(
        f"{command} {report.get('adapter_id')}: {verdict or 'done'} (backend {report.get('backend')})", file=sys.stderr
    )
    if command == "smoke":
        return 0 if verdict in ("smoke_passed", "stand_in_only") and all(c["passed"] for c in report["cases"]) else 1
    return 0
