# syntax=docker/dockerfile:1.7
# Execution services image (§9): orchestrator, scheduler, render-worker and worker-cpu (the
# `cpu_model` worker runtime with the mock adapters and the real CPU engines). One image keeps dev
# builds simple; per-service images are a Phase 14 hardening item. Model weights are never baked in:
# they come from the model cache volume (`make fetch-cpu-assets`, §36). Build from the repository root:
#   docker build -f infra/docker/services.Dockerfile -t creator-engine/services .
# Pinned by digest: Python 3.12.15 on Debian trixie (FFmpeg 7.1 with libass, HarfBuzz, FriBidi); uv 0.11.32.

FROM ghcr.io/astral-sh/uv@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c AS uv

FROM python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages/py ./packages/py
COPY apps ./apps
COPY plugins ./plugins
# Every plugin of the workspace (audit D6): the control plane routes and the scheduler accepts adapters
# only from the manifests installed here, so a GPU worker of another image registering with this
# scheduler had all its adapters dropped. The GPU adapters' packages are manifest + adapter code only
# (their engines live in the GPU family images, ADR 0052). tests/phase0 checks this list is complete.
RUN --mount=type=cache,target=/root/.cache/uv --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable --python /usr/local/bin/python3 \
      --package ce-orchestrator --package ce-scheduler --package ce-render-worker --package ce-gpu-worker \
      --package ce-plugin-asr-ctc-aligner --package ce-plugin-asr-qwen3 --package ce-plugin-audio-acestep \
      --package ce-plugin-audio-emotion --package ce-plugin-audio-moss-sfx \
      --package ce-plugin-avatar-infinitetalk --package ce-plugin-avatar-longcat \
      --package ce-plugin-embed-speaker --package ce-plugin-embed-text --package ce-plugin-image-qwen-edit \
      --package ce-plugin-image-z-image --package ce-plugin-lipsync-musetalk --package ce-plugin-qc-gpu \
      --package ce-plugin-translate-captions-llm --package ce-plugin-video-rife \
      --package ce-plugin-video-seedvr2 --package ce-plugin-video-wan22 --package ce-plugin-vision-vllm \
      --package ce-plugin-voice-chatterbox --package ce-plugin-voice-qwen3-tts \
      --package ce-plugin-voice-voxcpm2 --package ce-plugin-watermarks

FROM python@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS runtime
# FFmpeg renders and probes (ADR 0017: Debian's build is GPL — review before distributing this image);
# the Noto chain in assets/fonts renders captions (DejaVu stays as the last fallback). MediaPipe's
# native library links EGL/GLES and its audio module loads PortAudio, even on CPU (Phase 7).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig ca-certificates \
       libegl1 libgles2 libportaudio2 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 ce \
    && mkdir -p /models && chown ce:ce /models
COPY --from=build /app/.venv /app/.venv
COPY config /app/config
COPY assets /app/assets
# Director prompt templates (§13) and the fixture-LLM scenarios used in dev and test (§37); both
# are found next to CE_CONFIG_ROOT. Production uses a real LLM provider (startup refuses `fixture`).
COPY prompts /app/prompts
COPY eval/llm_fixtures /app/eval/llm_fixtures
# the golden evaluation set for the benchmark runner (Phase 11); its media are small and pinned
COPY eval/cases.yaml /app/eval/cases.yaml
COPY eval/smoke /app/eval/smoke
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 APP_ENV=prod CE_CONFIG_ROOT=/app/config \
    CE_FONTS_DIR=/app/assets/fonts MODEL_CACHE_DIR=/models
WORKDIR /app
USER ce
# The service is chosen by the command: python -m ce_orchestrator | ce_scheduler | ce_render_worker | ce_gpu_worker
CMD ["python", "-m", "ce_orchestrator"]
