"""Observability as code (§34, Phase 14): the Grafana dashboards are generated and fresh, every
dashboard query and alert rule names a metric the services define, and both Prometheus
configurations (Compose and native) scrape the same jobs."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml
from ce_obs.metrics import REGISTRY

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "infra" / "observability"
METRIC = re.compile(r"\b(ce_[a-z0-9_]+)")
SUFFIXES = ("", "_total", "_bucket", "_count", "_sum", "_created")


def defined_metrics() -> set[str]:
    return {family.name + suffix for family in REGISTRY.collect() for suffix in SUFFIXES}


def dashboard_queries() -> list[tuple[str, str]]:
    out = []
    for path in sorted((OBS / "grafana" / "dashboards").glob("*.json")):
        for panel in json.loads(path.read_text())["panels"]:
            out += [(f"{path.name}: {panel['title']}", t["expr"]) for t in panel.get("targets", [])]
    return out


def alert_rules() -> list[dict[str, Any]]:
    groups = yaml.safe_load((OBS / "alerts.yml").read_text())["groups"]
    return [rule for group in groups for rule in group["rules"]]


def test_dashboards_are_generated_and_fresh() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_dashboards.py"), "--check"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert len(dashboard_queries()) > 40


def test_every_query_and_alert_names_a_defined_metric() -> None:
    known = defined_metrics()
    used = [(where, name) for where, expr in dashboard_queries() for name in METRIC.findall(expr)]
    used += [(rule["alert"], name) for rule in alert_rules() for name in METRIC.findall(rule["expr"])]
    unknown = sorted({f"{where}: {name}" for where, name in used if name not in known})
    assert not unknown, unknown
    # and the ops gauges the scheduler leader sets are all on a dashboard
    on_dashboards = {name for _, expr in dashboard_queries() for name in METRIC.findall(expr)}
    ops = {f.name for f in REGISTRY.collect() if f.name.startswith("ce_ops_") and f.name != "ce_ops_collect_errors"}
    assert ops - {"ce_ops_window_seconds"} <= on_dashboards


def test_alerts_have_severity_and_runbook() -> None:
    from ce_testing import docs

    operations = docs.require("OPERATIONS.md").read_text().lower()
    for rule in alert_rules():
        assert rule["labels"]["severity"] in ("page", "ticket"), rule["alert"]
        runbook = rule["annotations"]["runbook"]
        doc, _, anchor = runbook.partition("#")
        assert doc == "docs/OPERATIONS.md" and f"### {anchor.replace('-', ' ')}" in operations, runbook


def test_compose_and_native_prometheus_scrape_the_same_jobs() -> None:
    def jobs(name: str) -> list[str]:
        return [j["job_name"] for j in yaml.safe_load((OBS / name).read_text())["scrape_configs"]]

    assert jobs("prometheus.yml") == jobs("prometheus.native.yml")
    alert_jobs = set(re.findall(r'job=~"([^"]+)"', (OBS / "alerts.yml").read_text()))
    for pattern in alert_jobs:
        assert set(pattern.split("|")) <= set(jobs("prometheus.yml"))
