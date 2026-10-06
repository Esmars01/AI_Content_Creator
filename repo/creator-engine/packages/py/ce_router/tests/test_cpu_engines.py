"""Real CPU engines in routing (§35 `CPU_REAL_ENGINES`, §1 rule 11): availability from the setting
and the assets, real implementations replacing mocks, and mock-made inputs kept on mocks."""

from __future__ import annotations

from pathlib import Path

import pytest
from ce_config.loader import load_config
from ce_contracts.manifest import cpu_engine_unavailable, missing_assets
from ce_contracts.plugins import cpu_engine_errors, discover
from ce_policy import OperatorProfile
from ce_router import RouteRequest, build_catalog, route
from ce_router.router import _passing

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
REGISTRY = discover(app_env="test", include_mocks=True)


def _catalog(mode: str, cache: Path | None):  # type: ignore[no-untyped-def]
    return build_catalog(
        REGISTRY,
        BUNDLE,
        app_env="test",
        mock_gpu=True,
        operator=OperatorProfile(),
        cpu_real_engines=mode,
        model_cache_dir=str(cache) if cache else None,
    )


def _fake_cache(tmp_path: Path, plugin_ids: list[str]) -> Path:
    for plugin_id in plugin_ids:
        for asset in REGISTRY.get(plugin_id).manifest.assets:
            path = tmp_path / asset
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
    return tmp_path


def test_off_keeps_every_real_cpu_engine_out(tmp_path: Path) -> None:
    catalog = _catalog("off", _fake_cache(tmp_path, ["kokoro_cpu", "faster_whisper_cpu"]))
    assert {"kokoro_cpu", "faster_whisper_cpu", "mediapipe_face", "prosody_features"} <= set(catalog.cpu_unavailable)
    assert route(RouteRequest(capability="voice.tts", language="en"), catalog).adapter_id == "mock_voice"


def test_auto_uses_engines_whose_assets_are_present(tmp_path: Path) -> None:
    catalog = _catalog("auto", _fake_cache(tmp_path, ["kokoro_cpu"]))
    assert "kokoro_cpu" not in catalog.cpu_unavailable
    assert "assets missing" in catalog.cpu_unavailable["faster_whisper_cpu"]
    assert "prosody_features" not in catalog.cpu_unavailable  # no assets to fetch: always usable in auto
    decision = route(RouteRequest(capability="voice.tts", language="en"), catalog)
    assert decision.adapter_id == "kokoro_cpu"
    passing, rejected = _passing(RouteRequest(capability="voice.tts", language="en"), catalog)
    assert [c.id for c in passing] == ["kokoro_cpu"]
    assert "mock replaced by the real implementation kokoro_cpu" in rejected["mock_voice"][0]
    # Kokoro has no German voice: the mock still serves de
    assert route(RouteRequest(capability="voice.tts", language="de"), catalog).adapter_id == "mock_voice"


def test_mock_made_input_stays_on_the_mock(tmp_path: Path) -> None:
    catalog = _catalog("auto", _fake_cache(tmp_path, ["faster_whisper_cpu"]))
    assert route(RouteRequest(capability="asr.transcribe", language="en"), catalog).adapter_id == "faster_whisper_cpu"
    assert (
        route(RouteRequest(capability="asr.transcribe", language="en", prefer_mock=True), catalog).adapter_id
        == "mock_asr"
    )


def test_real_signer_replaces_the_mock_signer() -> None:
    catalog = _catalog("off", None)
    assert route(RouteRequest(capability="provenance.sign"), catalog).adapter_id == "c2pa_signer"
    assert route(RouteRequest(capability="provenance.watermark_video"), catalog).adapter_id == "mock_provenance"


def test_on_requires_assets(tmp_path: Path) -> None:
    errors = cpu_engine_errors(REGISTRY, "on", str(tmp_path))
    assert any(e.startswith("kokoro_cpu:") for e in errors)
    assert cpu_engine_errors(REGISTRY, "auto", str(tmp_path)) == []
    manifest = REGISTRY.get("kokoro_cpu").manifest
    assert missing_assets(manifest, tmp_path) == manifest.assets
    assert cpu_engine_unavailable(REGISTRY.get("mock_voice").manifest, "on", None) is None  # mocks are not engines
    with pytest.raises(ValueError):
        cpu_engine_unavailable(manifest, "sometimes", None)
