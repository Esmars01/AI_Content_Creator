"""ce_config: the shipped config tree validates; layering, digests, cross-references, startup checks."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from ce_config.cli import app as config_app
from ce_config.loader import ConfigError, load_config, read_cube
from ce_config.settings import load_effective, startup_issues
from ce_core.yamlio import load_yaml_file
from typer.testing import CliRunner

ROOT = Path(__file__).resolve().parents[4]
CONFIG = ROOT / "config"

GOOD_PROD_ENV = {
    "APP_ENV": "prod",
    "SECRET_KEY": "x" * 48,
    "WORKER_TOKEN": "w" * 40,
    "MOCK_GPU": "false",
    "LLM_PROVIDER": "anthropic",
    "PROVENANCE_MODE": "",
    "COOKIE_SECURE": "",
}


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_shipped_config_has_no_issues(env: str) -> None:
    bundle = load_config(CONFIG, env)
    assert bundle.issues == [], [(i.code, i.path, i.message) for i in bundle.issues]


def test_shipped_config_contents() -> None:
    b = load_config(CONFIG, "dev")
    production_cams = {c for c, p in b.camera_profiles.items() if p.maturity == "production"}
    assert production_cams == {
        "phone_front_selfie",
        "phone_rear_handheld",
        "webcam",
        "laptop_camera",
        "desk_mirrorless",
        "dslr",
        "cinematic",
        "pov",
        "screen_webcam_bubble",
        "screen_only",
    }
    production_modes = {m for m, mode in b.modes.items() if mode.maturity == "production"}
    assert len(production_modes) == 12 and {"reaction", "cinematic_storytelling", "meme"} == {
        m for m, mode in b.modes.items() if mode.maturity == "beta"
    }
    assert set(b.strategy_packs) == {
        "hook_problem_payoff_cta",
        "myth_vs_reality",
        "listicle",
        "story_arc",
        "contrarian_take",
        "tutorial_steps",
    }
    assert set(b.platforms) == {"tiktok", "instagram_reels", "youtube_shorts", "youtube", "linkedin", "x"}
    assert all(p.verified_at is None for p in b.platforms.values()), "platform limits are unverified until Phase 12"
    assert set(b.routing) == {"draft", "final", "cheapest"}
    assert b.languages is not None and b.languages.languages["az"].support == "unsupported"
    assert b.render_preset("tiktok_1080x1920_30") is not None
    assert b.operator_profile is not None and b.operator_profile.jurisdiction == "EU"
    # the mock pool (simulated workers) and the local pool of the real CPU engines (Phase 7)
    assert b.gpu_pools is not None and [p.id for p in b.gpu_pools.pools if p.enabled] == ["mock", "cpu_local"]
    local = next(p for p in b.gpu_pools.pools if p.id == "cpu_local")
    assert local.providers == ["local"] and local.families == ["cpu_model"] and not local.autoscale


def test_env_layering_resolves_provenance_and_cookies() -> None:
    dev = load_effective(CONFIG, {"APP_ENV": "dev", "PROVENANCE_MODE": "", "COOKIE_SECURE": ""})
    assert (dev.provenance_mode, dev.cookie_secure) == ("mock_dev", False)
    prod = load_effective(CONFIG, GOOD_PROD_ENV)
    assert (prod.provenance_mode, prod.cookie_secure) == ("real", True)
    forced = load_effective(CONFIG, {"APP_ENV": "dev", "PROVENANCE_MODE": "real", "COOKIE_SECURE": "true"})
    assert (forced.provenance_mode, forced.cookie_secure) == ("real", True)


def test_explicit_environ_does_not_read_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "leaked-from-process")
    assert load_effective(CONFIG, {"APP_ENV": "test"}).settings.llm_model is None


def test_startup_checks() -> None:
    assert [i for i in startup_issues(load_effective(CONFIG, GOOD_PROD_ENV)) if i.severity == "error"] == []
    mock_prod = load_effective(CONFIG, GOOD_PROD_ENV | {"PROVENANCE_MODE": "mock_dev"})
    assert {i.code for i in startup_issues(mock_prod)} == {"provenance"}  # I11: prod refuses mock provenance
    insecure = load_effective(CONFIG, GOOD_PROD_ENV | {"COOKIE_SECURE": "false", "SECRET_KEY": "dev-only-change-me"})
    assert {i.code for i in startup_issues(insecure)} == {"cookie_secure", "secret_key"}
    local_storage = load_effective(CONFIG, GOOD_PROD_ENV | {"STORAGE_PROVIDER": "local_fs"})
    assert {i.code for i in startup_issues(local_storage)} == {"storage_provider"}  # ADR 0010, ADR 0030
    no_token = load_effective(CONFIG, {k: v for k, v in GOOD_PROD_ENV.items() if k != "WORKER_TOKEN"})
    assert {i.code for i in startup_issues(no_token)} == {"worker_token"}  # the SECRET_KEY-derived token is dev-only
    assert load_effective(CONFIG, {"S3_PUBLIC_ENDPOINT_URL": ""}).settings.s3_public_endpoint_url is None
    dev = load_effective(CONFIG, {"APP_ENV": "dev", "SECRET_KEY": "dev-only-change-me", "STORAGE_PROVIDER": "local_fs"})
    assert [i for i in startup_issues(dev) if i.severity == "error"] == []


def test_unknown_app_env_and_missing_root() -> None:
    with pytest.raises(ConfigError):
        load_config(CONFIG, "staging")
    with pytest.raises(ConfigError):
        load_config(CONFIG / "nowhere", "dev")


# ---------------------------------------------------------------------- digests


def copy_config(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(CONFIG, target)
    return target


def test_yaml_digest_ignores_comments_but_not_content(tmp_path: Path) -> None:
    root = copy_config(tmp_path)
    before = load_config(root).digests["camera_profiles/webcam.yaml"]
    path = root / "camera_profiles" / "webcam.yaml"
    path.write_text("# a new comment\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_config(root).digests["camera_profiles/webcam.yaml"] == before
    path.write_text(path.read_text(encoding="utf-8").replace("fps: 30", "fps: 25"), encoding="utf-8")
    assert load_config(root).digests["camera_profiles/webcam.yaml"] != before


def test_binary_assets_are_digested_and_combined_digest_is_stable() -> None:
    b = load_config(CONFIG)
    assert b.digests["luts/phone_natural.cube"].startswith("sha256:")
    assert b.digests["rooms/impulse_responses/small_office.wav"].startswith("sha256:")
    combined = b.digest_of("camera_profiles/phone_front_selfie.yaml", "luts/phone_natural.cube")
    assert combined == b.digest_of("luts/phone_natural.cube", "camera_profiles/phone_front_selfie.yaml")


# ---------------------------------------------------------------------- cross-references


@pytest.mark.parametrize(
    ("relative", "change", "code"),
    [
        ("camera_profiles/webcam.yaml", lambda d: d["color"].update(lut="luts/missing.cube"), "missing_ref"),
        ("camera_profiles/webcam.yaml", lambda d: d["audio"].update(mic_profile="tin_can"), "missing_ref"),
        ("camera_profiles/webcam.yaml", lambda d: d.update(id="webcam_v2"), "id_mismatch"),
        ("camera_profiles/webcam.yaml", lambda d: d["motion"].update(type="drone"), "schema"),
        ("modes/meme.yaml", lambda d: d.update(default_world_kinds=["spaceship"]), "unknown_vocab"),
        ("modes/meme.yaml", lambda d: d.update(strategy_packs=["viral_magic"]), "missing_ref"),
        ("modes/meme.yaml", lambda d: d.update(allowed_shot_types=["hologram"]), "schema"),
        ("modes/vlog.yaml", lambda d: d.update(maturity="production"), "maturity"),
        ("platforms/x.yaml", lambda d: d["render_presets"][0].update(height=1080), "aspect"),
        ("platforms/x.yaml", lambda d: d["render_presets"][0].update(id="tiktok_1080x1920_30"), "duplicate"),
        ("platforms/x.yaml", lambda d: d["rules"].update(max_duration_s=140), "unverified"),
        ("strategy_packs/listicle.yaml", lambda d: d.update(structure=["hook", "montage"]), "unknown_vocab"),
        ("memory.yaml", lambda d: d["retrieval"]["budgets"].pop("stance"), "missing_ref"),
        (
            "intent_policies.yaml",
            lambda d: d["rules"][0]["match"].update(reveal_strategy="slow_reveal"),
            "unknown_vocab",
        ),
        ("intent_policies.yaml", lambda d: d["rules"][0].update(group="nowhere"), "missing_ref"),
        ("qc/behavior.yaml", lambda d: d["must"]["warn_on"].append("HONORED_MAYBE"), "unknown_outcome"),
        ("rooms/cafe.yaml", lambda d: d.update(impulse_response="impulse_responses/none.wav"), "missing_ref"),
        ("languages.yaml", lambda d: d["languages"]["en"].update(support="beta"), "languages"),
        ("gpu/pools.yaml", lambda d: d["pools"][0].update(min=5), "range"),
        ("env/prod.yaml", lambda d: d["provenance"].update(mode="mock_dev"), "provenance"),
    ],
)
def test_cross_reference_rules(tmp_path: Path, relative: str, change: Callable[[Any], None], code: str) -> None:
    env = "prod" if relative.startswith("env/") else "dev"
    root = copy_config(tmp_path)
    data = load_yaml_file(root / relative)
    change(data)
    (root / relative).write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    codes = {i.code for i in load_config(root, env).issues}
    assert code in codes, codes


def test_model_registry_requires_licenses_and_rejects_non_commercial_production(tmp_path: Path) -> None:
    root = copy_config(tmp_path)
    license_nc = {
        "name": "CC-BY-NC-4.0",
        "url": "https://example.invalid/license",
        "commercial_use": False,
        "conditions": [{"non_commercial": True}],
        "verified_at": "2026-10-03",
        "verified_by": "test",
    }
    model = {
        "key": "demo",
        "adapter_id": "demo",
        "capability": "image.generate",
        "family": "image",
        "source": {"type": "huggingface", "repo": "org/demo", "revision": "abc123"},
        "license": license_nc,
        "status": "production",
        "validation": "untested_on_gpu",
    }
    (root / "models" / "demo.yaml").write_text(yaml.safe_dump({"models": [model]}), encoding="utf-8")
    assert "license" in {i.code for i in load_config(root).issues}
    del model["license"]
    (root / "models" / "demo.yaml").write_text(yaml.safe_dump({"models": [model]}), encoding="utf-8")
    assert "schema" in {i.code for i in load_config(root).issues}


def test_cube_parser(tmp_path: Path) -> None:
    info = read_cube(CONFIG / "luts" / "phone_natural.cube")
    assert info.size == 17 and info.title == "phone_natural"
    bad = tmp_path / "bad.cube"
    bad.write_text("LUT_3D_SIZE 2\n0 0 0\n1 1 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="needs 8 rows"):
        read_cube(bad)


def test_synthetic_assets_are_reproducible() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/gen_synthetic_assets.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_ffmpeg_accepts_the_luts_and_impulse_responses() -> None:
    """The synthetic assets work in the real render path (lut3d and afir filters)."""
    lut = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=s=64x64:d=0.1",
            "-vf",
            f"lut3d={CONFIG / 'luts' / 'cinematic_warm.cube'}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert lut.returncode == 0, lut.stderr
    ir = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=f=440:d=0.5",
            "-i",
            str(CONFIG / "rooms" / "impulse_responses" / "small_office.wav"),
            "-filter_complex",
            "[0][1]afir",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ir.returncode == 0, ir.stderr


def test_cli_validate_exit_codes(tmp_path: Path) -> None:
    runner = CliRunner()
    ok = runner.invoke(config_app, ["validate", "--root", str(CONFIG)])
    assert ok.exit_code == 0, ok.output
    assert "0 errors" in ok.output
    root = copy_config(tmp_path)
    (root / "modes" / "meme.yaml").write_text("id: meme\n", encoding="utf-8")
    bad = runner.invoke(config_app, ["validate", "--root", str(root)])
    assert bad.exit_code == 1
    assert "schema" in bad.output
