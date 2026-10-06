"""`python -m ce_render_worker`: the render queue's Temporal worker."""

from __future__ import annotations

import asyncio
import os
import sys

from ce_config.settings import load_effective
from ce_obs import configure_logging, configure_tracing, get_logger
from ce_obs.metrics import serve_metrics
from ce_orchestrator.worker import serve


def main() -> None:
    effective = load_effective(os.environ.get("CE_CONFIG_ROOT", "config"))
    configure_logging("render-worker", effective.settings.log_level)
    configure_tracing(
        "render-worker", effective.settings.otel_exporter_otlp_endpoint, environment=effective.settings.app_env
    )
    serve_metrics(int(os.environ.get("METRICS_PORT", "0")))
    try:
        asyncio.run(serve(effective, "render"))
    except RuntimeError as exc:
        get_logger("ce.render_worker").error("refusing to start", error=str(exc))
        sys.exit(2)


if __name__ == "__main__":
    main()
