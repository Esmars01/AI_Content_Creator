"""Phase 0: `.env.example` is complete and dev-safe; the layered config files parse; Makefile targets exist."""

from __future__ import annotations

import re
import subprocess

import yaml

from tests.phase0 import spec_index as spec

ROOT = spec.ROOT


def env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_env_example_lists_every_section_35_variable() -> None:
    values = env_example()
    missing = [v for v in spec.env_vars() if v not in values]
    assert not missing, f".env.example lacks: {missing}"


def test_env_example_is_dev_safe() -> None:
    values = env_example()
    assert values["APP_ENV"] == "dev"
    # §35: environment-dependent settings stay empty and resolve from config/env/<APP_ENV>.yaml.
    assert values["PROVENANCE_MODE"] == ""
    assert values["COOKIE_SECURE"] == ""
    assert values["MOCK_GPU"] == "true"
    assert values["LLM_PROVIDER"] == "fixture"
    # Rule 16: no real credentials. Paid-provider keys must be empty.
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPAT_API_KEY",
        "HF_TOKEN",
        "RUNPOD_API_KEY",
        "VAST_API_KEY",
        "SENTRY_DSN",
    ):
        assert values[key] == "", f"{key} must be empty in .env.example"
    assert "dev" in values["SECRET_KEY"], "SECRET_KEY in .env.example must be an obviously dev-only value"


def test_spend_caps_match_spec_defaults() -> None:
    values = env_example()
    assert values["BUDGET_DAILY_USD"] == "20"
    assert values["SMOKE_SPEND_CAP_USD"] == "5"


def test_layered_config_files_parse_and_resolve_env_dependent_values() -> None:
    default = yaml.safe_load((ROOT / "config" / "default.yaml").read_text(encoding="utf-8"))
    envs = {
        name: yaml.safe_load((ROOT / "config" / "env" / f"{name}.yaml").read_text(encoding="utf-8"))
        for name in ("dev", "test", "prod")
    }
    assert isinstance(default, dict)

    def resolved(env: str, section: str, key: str) -> object:
        return {**default.get(section, {}), **envs[env].get(section, {})}[key]

    assert resolved("dev", "security", "cookie_secure") is False
    assert resolved("test", "security", "cookie_secure") is False
    assert resolved("prod", "security", "cookie_secure") is True
    assert resolved("dev", "provenance", "mode") == "mock_dev"
    assert resolved("test", "provenance", "mode") == "mock_dev"
    assert resolved("prod", "provenance", "mode") == "real"


def test_makefile_defines_every_section_36_target() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    defined = set(re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.M))
    missing = [t for t in spec.make_targets() if t not in defined]
    assert not missing, f"Makefile lacks §36 targets: {missing}"


def test_no_section_36_target_is_still_a_placeholder() -> None:
    """Every §36 target is implemented since Phase 8; the per-adapter targets fail loudly without PLUGIN."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "$(call not_yet" not in makefile
    result = subprocess.run(["make", "-s", "smoke-gpu"], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "usage: make smoke-gpu PLUGIN=" in result.stderr


def test_compose_core_profile_has_healthchecks_and_pinned_images() -> None:
    compose = yaml.safe_load((ROOT / "infra" / "compose" / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    infrastructure = {"postgres", "redis", "temporal", "temporal-ui", "seaweedfs"}
    applications = {"api-migrate", "api", "orchestrator", "scheduler", "render-worker", "web"}  # Phases 1, 2, 5
    workers = {"worker-cpu"}  # profile mock-gpu (Phase 2); cpu-real joins in Phase 7
    observability = {"otel-collector", "prometheus", "alertmanager", "grafana", "loki", "tempo"}  # Phase 14
    assert set(services) == infrastructure | applications | workers | observability
    for name, service in services.items():
        profile = "mock-gpu" if name in workers else "observability" if name in observability else "core"
        assert profile in service.get("profiles", []), f"{name} is not in the {profile} profile"
        if name in infrastructure | observability:
            assert "@sha256:" in service["image"], f"{name} image is not pinned by digest"
        else:
            dockerfile = ROOT / "infra" / "compose" / service["build"]["context"] / service["build"]["dockerfile"]
            assert dockerfile.is_file(), f"{name}: {dockerfile} missing"
            base_images = [
                line for line in dockerfile.read_text(encoding="utf-8").splitlines() if line.startswith("FROM ")
            ]
            assert base_images and all("@sha256:" in line for line in base_images), f"{name}: base images not pinned"
        # a one-shot job has no health (dependants wait for its completion); the Loki image has no
        # shell, HTTP client or health subcommand to probe with
        if name not in ("api-migrate", "loki"):
            assert "healthcheck" in service, f"{name} has no healthcheck"


def test_fetch_cpu_assets_manifest_pins_every_asset() -> None:
    """Every CPU asset pins a URL, a SHA-256, its size and its verified license (rule 15), and each
    file a plugin manifest lists is in the fetch manifest (Phase 7)."""
    import yaml
    from ce_contracts.plugins import discover

    assets = yaml.safe_load((ROOT / "scripts" / "cpu_assets.yaml").read_text(encoding="utf-8"))["assets"]
    for asset in assets:
        for field in ("name", "url", "sha256", "dest", "license", "plugins"):
            assert asset.get(field), f"{asset.get('name')}: missing {field}"
        assert len(asset["sha256"]) == 64 and asset["url"].startswith("https://")
    listed = {a["dest"] for a in assets}
    registry = discover(app_env=None, include_mocks=False)
    for plugin in registry.plugins.values():
        assert set(plugin.manifest.assets) <= listed, f"{plugin.id}: assets missing from scripts/cpu_assets.yaml"
    result = subprocess.run(
        ["uv", "run", "python", "scripts/fetch_cpu_assets.py", "--check", "--plugin", "no_such_plugin"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "Nothing to fetch" in result.stdout, result.stdout + result.stderr
