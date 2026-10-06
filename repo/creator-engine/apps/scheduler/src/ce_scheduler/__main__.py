"""`python -m ce_scheduler`: serve the worker API and run the leader loops."""

from __future__ import annotations

import os
import sys

import uvicorn
from ce_config.settings import load_effective
from ce_obs import configure_logging, configure_tracing, get_logger
from ce_obs.metrics import serve_metrics

from ce_scheduler.app import create_app


def main() -> None:
    effective = load_effective(os.environ.get("CE_CONFIG_ROOT", "config"))
    configure_logging("scheduler", effective.settings.log_level)
    try:
        app = create_app(effective)
    except RuntimeError as exc:
        get_logger("ce.scheduler").error("refusing to start", error=str(exc))
        sys.exit(2)
    # metrics on a side port: the worker API port faces remote GPU workers (§25), metrics stay internal
    serve_metrics(int(os.environ.get("METRICS_PORT", "0")))
    configure_tracing(
        "scheduler", effective.settings.otel_exporter_otlp_endpoint, environment=effective.settings.app_env
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SCHEDULER_PORT", "8100")), log_level="warning")  # noqa: S104


if __name__ == "__main__":
    main()
