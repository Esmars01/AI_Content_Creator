# ruff: noqa: E501 - PromQL queries are kept on one line each
"""Generates the Grafana dashboards in infra/observability/grafana/dashboards (§34, Phase 14).

Dashboards are code: edit the panels here and run `make gen-dashboards`; a test checks the JSON
is fresh and that every query names a metric `ce_obs.metrics` defines.

    uv run python scripts/gen_dashboards.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "infra" / "observability" / "grafana" / "dashboards"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}


@dataclass
class Panel:
    title: str
    exprs: list[tuple[str, str]]  # (PromQL or LogQL, legend)
    unit: str = "short"
    kind: str = "timeseries"  # timeseries | stat | logs | table
    width: int = 12
    height: int = 8
    description: str = ""
    stack: bool = False


@dataclass
class Dashboard:
    uid: str
    title: str
    description: str
    rows: list[tuple[str, list[Panel]]] = field(default_factory=list)


def _panel(p: Panel, pid: int, x: int, y: int) -> dict[str, Any]:
    datasource = LOKI if p.kind == "logs" else PROM
    out: dict[str, Any] = {
        "id": pid,
        "type": p.kind,
        "title": p.title,
        "description": p.description,
        "datasource": datasource,
        "gridPos": {"h": p.height, "w": p.width, "x": x, "y": y},
        "targets": [
            {"refId": chr(ord("A") + i), "expr": expr, "legendFormat": legend, "datasource": datasource}
            for i, (expr, legend) in enumerate(p.exprs)
        ],
    }
    if p.kind != "logs":
        custom: dict[str, Any] = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 10, "showPoints": "never"}
        if p.stack:
            custom["stacking"] = {"mode": "normal", "group": "A"}
        out["fieldConfig"] = {"defaults": {"unit": p.unit, "custom": custom if p.kind == "timeseries" else {}}}
        out["options"] = {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}
    if p.kind == "stat":
        out["options"] = {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value", "graphMode": "area"}
    if p.kind == "logs":
        out["options"] = {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"}
    return out


def render(d: Dashboard) -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    y, pid = 0, 1
    for row_title, row_panels in d.rows:
        panels.append(
            {
                "id": pid,
                "type": "row",
                "title": row_title,
                "collapsed": False,
                "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
                "panels": [],
            }
        )
        pid += 1
        y += 1
        x = 0
        row_height = 0
        for p in row_panels:
            if x + p.width > 24:
                x, y = 0, y + row_height
                row_height = 0
            panels.append(_panel(p, pid, x, y))
            pid += 1
            x += p.width
            row_height = max(row_height, p.height)
        y += row_height
    return {
        "uid": d.uid,
        "title": d.title,
        "description": d.description,
        "tags": ["creator-engine"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 1,
        "editable": False,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {"list": []},
        "annotations": {"list": []},
        "panels": panels,
    }


RATE = "[5m]"

DASHBOARDS = [
    Dashboard(
        "ce-overview",
        "Creator Engine — overview",
        "Service health, API traffic, errors and latency.",
        [
            (
                "Health",
                [
                    Panel(
                        "Services up",
                        [('up{job=~"api|scheduler|orchestrator|render-worker|worker"}', "{{job}}")],
                        kind="stat",
                        width=8,
                    ),
                    Panel(
                        "API requests/s",
                        [(f'sum(rate(ce_http_requests_total{{service="api"}}{RATE}))', "req/s")],
                        "reqps",
                        "stat",
                        8,
                    ),
                    Panel(
                        "API 5xx ratio",
                        [
                            (
                                f'sum(rate(ce_http_requests_total{{service="api",status=~"5.."}}{RATE})) / clamp_min(sum(rate(ce_http_requests_total{{service="api"}}{RATE})), 1e-9)',
                                "5xx",
                            )
                        ],
                        "percentunit",
                        "stat",
                        8,
                    ),
                ],
            ),
            (
                "API",
                [
                    Panel(
                        "Requests by status",
                        [(f'sum by (status) (rate(ce_http_requests_total{{service="api"}}{RATE}))', "{{status}}")],
                        "reqps",
                        stack=True,
                    ),
                    Panel(
                        "Latency p50 / p95 / p99",
                        [
                            (
                                f'histogram_quantile({q}, sum by (le) (rate(ce_http_request_seconds_bucket{{service="api"}}{RATE})))',
                                f"p{int(q * 100)}",
                            )
                            for q in (0.5, 0.95, 0.99)
                        ],
                        "s",
                    ),
                    Panel(
                        "Slowest routes (p95)",
                        [
                            (
                                f'topk(10, histogram_quantile(0.95, sum by (le, method, route) (rate(ce_http_request_seconds_bucket{{service="api"}}{RATE}))))',
                                "{{method}} {{route}}",
                            )
                        ],
                        "s",
                        width=24,
                    ),
                    Panel(
                        "Errors by route",
                        [
                            (
                                f'topk(10, sum by (method, route, status) (rate(ce_http_requests_total{{service="api",status=~"4..|5.."}}{RATE})))',
                                "{{status}} {{method}} {{route}}",
                            )
                        ],
                        "reqps",
                        width=24,
                    ),
                ],
            ),
            (
                "Logs",
                [
                    Panel(
                        "Warnings and errors",
                        [('{service_name=~".+"} | json | level=~"warning|error"', "")],
                        kind="logs",
                        width=24,
                        height=10,
                    )
                ],
            ),
        ],
    ),
    Dashboard(
        "ce-scheduler",
        "Creator Engine — scheduler, queue and workers",
        "GPU queue depth, leases, waits, task outcomes, workers, fleet and providers.",
        [
            (
                "Queue",
                [
                    Panel("Tasks by state", [("sum by (state) (ce_gpu_tasks)", "{{state}}")], stack=True),
                    Panel(
                        "Queued by capability",
                        [('sum by (capability) (ce_gpu_tasks{state="queued"})', "{{capability}}")],
                        stack=True,
                    ),
                    Panel(
                        "Lease wait p50 / p90",
                        [
                            (
                                f"histogram_quantile({q}, sum by (le) (rate(ce_gpu_lease_wait_seconds_bucket{RATE})))",
                                f"p{int(q * 100)}",
                            )
                            for q in (0.5, 0.9)
                        ],
                        "s",
                    ),
                    Panel(
                        "Leases/s by capability",
                        [(f"sum by (capability) (rate(ce_gpu_leases_total{RATE}))", "{{capability}}")],
                        "ops",
                    ),
                    Panel(
                        "Task outcomes",
                        [(f"sum by (status) (rate(ce_gpu_tasks_done_total{RATE}))", "{{status}}")],
                        "ops",
                        stack=True,
                    ),
                    Panel("Expired leases", [(f"sum(increase(ce_gpu_lease_expired_total{RATE}))", "expired")]),
                    Panel("Held by budget", [("sum by (reason) (ce_gpu_tasks_held)", "{{reason}}")]),
                    Panel(
                        "Attempts by reason / error (window)",
                        [("sum by (reason, error_class) (ce_ops_attempts)", "{{reason}} {{error_class}}")],
                        kind="table",
                    ),
                ],
            ),
            (
                "Workers",
                [
                    Panel("Workers by state", [("sum by (state) (ce_workers)", "{{state}}")], stack=True),
                    Panel("Desired workers per pool", [("ce_fleet_desired_workers", "{{pool}}")]),
                    Panel(
                        "Worker task outcomes",
                        [(f"sum by (status) (rate(ce_worker_tasks_total{RATE}))", "{{status}}")],
                        "ops",
                        stack=True,
                    ),
                    Panel(
                        "Cold start p90",
                        [
                            (
                                f"histogram_quantile(0.9, sum by (le, gpu_type) (rate(ce_worker_cold_start_seconds_bucket{RATE})))",
                                "{{gpu_type}}",
                            )
                        ],
                        "s",
                    ),
                    Panel(
                        "Model load p90",
                        [
                            (
                                f"histogram_quantile(0.9, sum by (le, adapter) (rate(ce_model_load_seconds_bucket{RATE})))",
                                "{{adapter}}",
                            )
                        ],
                        "s",
                    ),
                    Panel(
                        "Model fetch p90",
                        [
                            (
                                f"histogram_quantile(0.9, sum by (le, adapter) (rate(ce_model_fetch_seconds_bucket{RATE})))",
                                "{{adapter}}",
                            )
                        ],
                        "s",
                    ),
                    Panel(
                        "Provisioning by provider / result",
                        [
                            (
                                "sum by (provider, result) (increase(ce_fleet_provisions_total[1h]))",
                                "{{provider}} {{result}}",
                            )
                        ],
                        width=24,
                    ),
                ],
            ),
        ],
    ),
    Dashboard(
        "ce-build",
        "Creator Engine — builds, renders and models",
        "Build nodes by kind and outcome, cache hit rate, node and render times, per-model QC and retries.",
        [
            (
                "Build",
                [
                    Panel(
                        "Cache hit rate",
                        [
                            (
                                f'sum(rate(ce_build_nodes_total{{outcome="cached"}}{RATE})) / clamp_min(sum(rate(ce_build_nodes_total{{outcome=~"cached|executed"}}{RATE})), 1e-9)',
                                "hit rate",
                            )
                        ],
                        "percentunit",
                        "stat",
                        8,
                    ),
                    Panel(
                        "Node failures",
                        [(f'sum(increase(ce_build_nodes_total{{outcome!~"cached|executed"}}{RATE}))', "failed")],
                        kind="stat",
                        width=8,
                    ),
                    Panel("Nodes/s", [(f"sum(rate(ce_build_nodes_total{RATE}))", "nodes/s")], "ops", "stat", 8),
                    Panel(
                        "Nodes by outcome",
                        [(f"sum by (outcome) (rate(ce_build_nodes_total{RATE}))", "{{outcome}}")],
                        "ops",
                        stack=True,
                    ),
                    Panel(
                        "Node time p90 by kind",
                        [
                            (
                                f"topk(10, histogram_quantile(0.9, sum by (le, kind) (rate(ce_build_node_seconds_bucket{RATE}))))",
                                "{{kind}}",
                            )
                        ],
                        "s",
                    ),
                    Panel(
                        "Render time p90",
                        [
                            (
                                f"histogram_quantile(0.9, sum by (le, kind) (rate(ce_render_seconds_bucket{RATE})))",
                                "{{kind}}",
                            )
                        ],
                        "s",
                    ),
                    Panel(
                        "Failures by node kind",
                        [
                            (
                                'sum by (kind, outcome) (increase(ce_build_nodes_total{outcome!~"cached|executed"}[1h]))',
                                "{{kind}} {{outcome}}",
                            )
                        ],
                    ),
                ],
            ),
            (
                "Models (window: ce_ops_window_seconds)",
                [
                    Panel(
                        "Shot QC fail rate by adapter",
                        [
                            (
                                'sum by (adapter) (ce_ops_qc_shots{verdict="fail"}) / clamp_min(sum by (adapter) (ce_ops_qc_shots), 1)',
                                "{{adapter}}",
                            )
                        ],
                        "percentunit",
                    ),
                    Panel(
                        "Shot QC by adapter × language",
                        [
                            (
                                "sum by (adapter, language, verdict) (ce_ops_qc_shots)",
                                "{{adapter}} {{language}} {{verdict}}",
                            )
                        ],
                        kind="table",
                    ),
                    Panel(
                        "Retries (infra / QC / fallback)",
                        [('sum by (reason) (ce_ops_attempts{reason!="initial"})', "{{reason}}")],
                    ),
                    Panel("OOM attempts", [('sum(ce_ops_attempts{error_class="oom"})', "oom")], kind="stat"),
                ],
            ),
        ],
    ),
    Dashboard(
        "ce-quality",
        "Creator Engine — quality, continuity and memory",
        "Behavior coverage outcomes, NOT_MEASURABLE rate, world and continuity QC, creator consistency, memory conflicts. All from ce_ops_* (rows in the trailing window).",
        [
            (
                "Behavior coverage",
                [
                    Panel(
                        "Delivered (*_CONFIRMED) share by adapter",
                        [
                            (
                                'sum by (adapter) (ce_ops_coverage_observations{outcome=~".*_CONFIRMED"}) / clamp_min(sum by (adapter) (ce_ops_coverage_observations), 1)',
                                "{{adapter}}",
                            )
                        ],
                        "percentunit",
                    ),
                    Panel(
                        "NOT_MEASURABLE rate",
                        [("sum(ce_ops_not_measurable) / clamp_min(sum(ce_ops_coverage_observations), 1)", "rate")],
                        "percentunit",
                        "stat",
                    ),
                    Panel(
                        "Outcomes by adapter × dimension",
                        [
                            (
                                "sum by (adapter, dimension, outcome) (ce_ops_coverage_observations)",
                                "{{adapter}} {{dimension}} {{outcome}}",
                            )
                        ],
                        kind="table",
                        width=24,
                    ),
                    Panel("NOT_MEASURABLE by dimension", [("ce_ops_not_measurable", "{{dimension}}")], kind="table"),
                ],
            ),
            (
                "Worlds and creators",
                [
                    Panel("World / continuity QC", [("sum by (verdict) (ce_ops_world_qc)", "{{verdict}}")], stack=True),
                    Panel(
                        "Consistency verdicts", [("sum by (verdict) (ce_ops_consistency)", "{{verdict}}")], stack=True
                    ),
                    Panel(
                        "Out-of-band ratio",
                        [
                            (
                                'sum(ce_ops_consistency{verdict="out_of_band"}) / clamp_min(sum(ce_ops_consistency), 1)',
                                "out of band",
                            )
                        ],
                        "percentunit",
                        "stat",
                    ),
                    Panel("Unresolved memory conflicts", [("ce_ops_memory_conflicts_open", "open")], kind="stat"),
                ],
            ),
        ],
    ),
    Dashboard(
        "ce-cost",
        "Creator Engine — cost",
        "Daily fleet spend against the budget, ledger spend and cost per output minute. Prices come from the providers' reported rates and the cost ledger; nothing here is an estimate of a provider bill.",
        [
            (
                "Spend",
                [
                    Panel("Fleet spend today vs budget", [("ce_fleet_spend_usd", "{{kind}}")], "currencyUSD"),
                    Panel("Ledger spend (window)", [("ce_ops_spend_usd", "spend")], "currencyUSD", "stat", 6),
                    Panel(
                        "Cost per output minute (window)",
                        [("ce_ops_spend_usd / (ce_ops_rendered_minutes > 0)", "USD/min")],
                        "currencyUSD",
                        "stat",
                        6,
                    ),
                    Panel("Rendered minutes (window)", [("ce_ops_rendered_minutes", "minutes")], kind="stat", width=6),
                    Panel("Tasks held by budget", [("sum by (reason) (ce_gpu_tasks_held)", "{{reason}}")], width=6),
                ],
            ),
        ],
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed JSON is stale")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    stale = []
    for d in DASHBOARDS:
        text = json.dumps(render(d), indent=2, sort_keys=True) + "\n"
        path = OUT / f"{d.uid}.json"
        if args.check:
            if not path.exists() or path.read_text() != text:
                stale.append(path.name)
        else:
            path.write_text(text)
    if stale:
        print("stale dashboards (run `make gen-dashboards`):", ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
