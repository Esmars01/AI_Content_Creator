# syntax=docker/dockerfile:1.7
# Control-plane image: the `api` service (§9) and the `ce` admin CLI. Build from the repository root:
#   docker build -f infra/docker/api.Dockerfile -t creator-engine/api .
# Pinned by digest: Python 3.12.15 on Debian trixie; uv 0.11.32 (matches uv.lock).

FROM ghcr.io/astral-sh/uv@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
# Built where it runs (/app/.venv): console scripts keep a valid interpreter path.
WORKDIR /src
# Workspace metadata first (layer cache), then sources.
COPY pyproject.toml uv.lock ./
COPY packages/py ./packages/py
COPY apps ./apps
COPY plugins ./plugins
# Optional build secret `extra_ca`: a CA bundle for TLS-intercepting proxies (corporate or sandboxed builds).
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable --package ce-api --python /usr/local/bin/python3

FROM python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS runtime
# ffprobe validates uploads (§33). Debian's FFmpeg is a GPL build: see ADR 0017 before distributing this image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ce
COPY --from=build /app/.venv /app/.venv
COPY config /app/config
COPY packages/py/ce_db/alembic.ini /app/alembic.ini
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 APP_ENV=prod
WORKDIR /app
USER ce
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --retries=6 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]
# Forwarded headers are trusted only from FORWARDED_ALLOW_IPS (uvicorn; default 127.0.0.1): the client
# address feeds login rate limiting, so set it to the load balancer's address in production.
# The app logs one JSON line per request (ce_obs), so uvicorn's access log is off.
CMD ["uvicorn", "--factory", "ce_api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
