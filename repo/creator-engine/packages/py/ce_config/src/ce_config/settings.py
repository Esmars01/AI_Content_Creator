"""Environment settings (§35) and the effective, layered configuration.

Precedence: `config/default.yaml` → `config/env/<APP_ENV>.yaml` → environment variables →
DB runtime settings (feature flags, operator profile; Phase 1 API). Variables left empty in
`.env.example` (`PROVENANCE_MODE`, `COOKIE_SECURE`) resolve from the config files.
`startup_issues()` enforces the production values; services refuse to start on errors.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from ce_core.errors import Issue
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ce_config.loader import ConfigBundle, load_config

__all__ = ["DEV_SECRET_KEYS", "EffectiveConfig", "Settings", "load_effective", "startup_issues"]

DEV_SECRET_KEYS = frozenset({"dev-only-change-me", "change-me", "secret", ""})


def _empty_to_none(value: object) -> object:
    return None if value == "" else value


class Settings(BaseSettings):
    """Every §35 environment variable. Names match exactly (case-insensitive)."""

    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    secret_key: SecretStr = SecretStr("")
    database_url: str = "postgresql+asyncpg://localhost/creator_engine"
    redis_url: str = "redis://localhost:6379/0"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue_prefix: str = ""  # not in §35: isolates parallel test sessions on one Temporal server
    s3_endpoint_url: str = "http://localhost:8333"
    s3_region: str = "us-east-1"
    s3_access_key_id: SecretStr = SecretStr("")
    s3_secret_access_key: SecretStr = SecretStr("")
    s3_bucket_assets: str = "ce-assets"
    s3_bucket_artifacts: str = "ce-artifacts"
    # Not in §35 (D21, ADR 0030): the presign endpoint browsers reach, and the native-fallback storage.
    s3_public_endpoint_url: str | None = None
    storage_provider: Literal["s3", "local_fs"] = "s3"
    local_storage_root: str = "./.data/storage"
    public_base_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"
    scheduler_public_url: str = "http://localhost:8100"
    scheduler_internal_url: str = "http://localhost:8100"  # the API → scheduler fleet endpoints (Phase 9)
    mock_gpu: bool = True
    mock_qc_fail_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    cpu_real_engines: Literal["auto", "on", "off"] = "auto"
    llm_provider: str = "fixture"
    llm_model: str | None = None
    anthropic_api_key: SecretStr | None = None
    openai_compat_base_url: str | None = None
    openai_compat_api_key: SecretStr | None = None
    embedding_model: str | None = None
    embedding_dim: int = Field(default=1024, ge=1, le=2000)
    hf_token: SecretStr | None = None
    model_cache_dir: str = "/models"
    runpod_api_key: SecretStr | None = None
    vast_api_key: SecretStr | None = None
    worker_token: SecretStr | None = None
    budget_daily_usd: float = Field(default=20.0, ge=0.0)
    smoke_spend_cap_usd: float = Field(default=5.0, ge=0.0)
    operator_jurisdiction: str = "EU"
    operator_revenue_band: Literal["lt_1m", "1m_10m", "gte_10m"] = "lt_1m"
    provenance_mode: Literal["real", "mock_dev"] | None = None
    c2pa_signing_cert_path: str | None = None
    c2pa_signing_key_path: str | None = None
    visible_label_default: Literal["auto", "on", "off"] = "auto"
    kms_provider: str = "local"
    kms_local_key_path: str | None = None
    cookie_secure: bool | None = None
    idempotency_ttl_s: int = Field(default=86_400, gt=0)
    sse_stream_retention_s: int = Field(default=86_400, gt=0)
    enrollment_token_ttl_s: int = Field(default=3_600, gt=0)
    fixture_clips_dir: str | None = None
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: SecretStr | None = None

    @field_validator(
        "llm_model",
        "anthropic_api_key",
        "openai_compat_base_url",
        "openai_compat_api_key",
        "embedding_model",
        "hf_token",
        "runpod_api_key",
        "vast_api_key",
        "worker_token",
        "provenance_mode",
        "c2pa_signing_cert_path",
        "c2pa_signing_key_path",
        "kms_local_key_path",
        "cookie_secure",
        "fixture_clips_dir",
        "otel_exporter_otlp_endpoint",
        "sentry_dsn",
        "s3_public_endpoint_url",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        return _empty_to_none(value)


class _ExplicitSettings(Settings):
    """Reads only the values passed in, never the process environment (for explicit `environ`)."""

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):  # type: ignore[no-untyped-def,override]
        return (init_settings,)


@dataclass(frozen=True)
class EffectiveConfig:
    settings: Settings
    bundle: ConfigBundle
    provenance_mode: Literal["real", "mock_dev"]
    cookie_secure: bool


def load_effective(config_root: Path | str = "config", environ: Mapping[str, str] | None = None) -> EffectiveConfig:
    env = dict(os.environ if environ is None else environ)
    values = {k.lower(): v for k, v in env.items() if k.lower() in Settings.model_fields}
    settings: Settings = _ExplicitSettings(**cast(dict[str, Any], values)) if environ is not None else Settings()
    bundle = load_config(config_root, settings.app_env)
    provenance = settings.provenance_mode or bundle.app.provenance.mode
    cookie_secure = settings.cookie_secure if settings.cookie_secure is not None else bundle.app.security.cookie_secure
    return EffectiveConfig(settings, bundle, provenance, cookie_secure)


def startup_issues(effective: EffectiveConfig) -> list[Issue]:
    """Refusals at service start (§35 production checks, I11). Errors must stop the service."""
    s = effective.settings
    issues: list[Issue] = []

    def error(code: str, message: str) -> None:
        issues.append(Issue(code, message))

    if effective.provenance_mode == "mock_dev" and s.app_env not in ("dev", "test"):
        error("provenance", "PROVENANCE_MODE=mock_dev is only allowed in dev and test (I11, ADR 0013)")
    if s.app_env == "prod":
        if effective.provenance_mode != "real":
            error("provenance", "production requires real provenance (I11)")
        if not effective.cookie_secure:
            error("cookie_secure", "production requires COOKIE_SECURE=true")
        key = s.secret_key.get_secret_value()
        if key in DEV_SECRET_KEYS or len(key) < 32:
            error("secret_key", "production requires a SECRET_KEY of at least 32 characters that is not a dev value")
        if s.mock_gpu:
            error("mock_gpu", "production must not register the mock GPU provider (MOCK_GPU=false)")
        if s.llm_provider == "fixture":
            error("llm_provider", "production must not use the fixture LLM provider")
        if s.storage_provider != "s3":
            error("storage_provider", "production stores objects through the S3 API only (ADR 0010, ADR 0030)")
        token = s.worker_token.get_secret_value() if s.worker_token else ""
        if len(token) < 32:
            error("worker_token", "production requires a WORKER_TOKEN of at least 32 characters (worker registration)")
    else:
        if not s.secret_key.get_secret_value():
            issues.append(Issue("secret_key", "SECRET_KEY is empty; sessions will not be signed", severity="warning"))
    issues += effective.bundle.errors
    return issues
