"""Router (§23): hard filters, scoring, pinning, user pins, fallbacks and the route preview."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from ce_config.loader import load_config
from ce_contracts.manifest import PluginManifest
from ce_contracts.plugins import discover
from ce_core.build import RouteDecision
from ce_policy import OperatorProfile
from ce_router import MeasuredProfile, RouterCatalog, RouteRequest, RoutingError, build_catalog, preview, route

ROOT = Path(__file__).resolve().parents[4]
BUNDLE = load_config(ROOT / "config", "test")
REGISTRY = discover(app_env="test", include_mocks=True)
assert BUNDLE.gpu_pools is not None
POOLS = BUNDLE.gpu_pools.pools


def catalog(**overrides: Any) -> RouterCatalog:
    base = build_catalog(REGISTRY, BUNDLE, app_env="test", mock_gpu=True, operator=OperatorProfile())
    return replace(base, **overrides)


def real_manifest(adapter_id: str = "avatar.fake_real", **changes: Any) -> PluginManifest:
    """A non-mock manifest shaped like a GPU avatar adapter (no weights are touched)."""
    data: dict[str, Any] = {
        "id": adapter_id,
        "name": "Fake real avatar",
        "version": "0.1.0",
        "kind": "model_adapter",
        "entrypoint": "fake.module:Adapter",
        "behavior_translator": "fake.module:Translator",
        "capabilities": [
            {
                "id": "avatar.a2v",
                "features": ["i2v"],
                "languages": {"mode": "audio_driven", "validated": ["en", "zh"], "unvalidated": ["de", "tr", "az"]},
                "resolutions": ["480p", "720p"],
                "max_duration_s": 60,
            }
        ],
        "behavior_matrix": {"emotion_visual": {"control": "text_global", "temporal_precision": "clip"}},
        "runtime": {"family": "wan", "requires_gpu": True, "min_vram_gb": 24, "supports_cpu": False},
        "models": [
            {
                "key": "fake-real",
                "source": {"type": "huggingface", "repo": "x/y", "revision": "abc"},
                "license": {
                    "name": "Apache-2.0",
                    "url": "u",
                    "commercial_use": True,
                    "verified_at": "2026-10-03",
                    "verified_by": "test",
                },
            }
        ],
        "status": "production",
        "validation": "smoke_passed",
        "allowed_envs": ["dev", "test", "prod"],
    }
    data.update(changes)
    return PluginManifest.model_validate(data)


def with_manifests(*extra: PluginManifest, **overrides: Any) -> RouterCatalog:
    base = catalog(**overrides)
    return replace(base, manifests={**base.manifests, **{m.id: m for m in extra}})


AVATAR = RouteRequest(capability="avatar.a2v", routing_profile="draft", language="en-US", height=720, work_units=6)


def test_mock_avatar_routes_with_mock_gpu() -> None:
    decision = route(AVATAR, catalog())
    assert decision.adapter_id in {"mock_avatar_global", "mock_avatar_segment"}
    assert decision.model_id.startswith("mock-avatar") and decision.revision == "1"
    assert decision.translator_version and decision.score is not None
    assert decision.fallbacks and decision.adapter_id not in decision.fallbacks


def test_mocks_are_not_routed_without_mock_gpu() -> None:
    with pytest.raises(RoutingError) as err:
        route(AVATAR, catalog(mock_gpu=False))
    assert "mock adapter while MOCK_GPU=false" in str(err.value)


def test_language_rule_validated_unvalidated_and_unsupported() -> None:
    assert route(replace(AVATAR, language="tr-TR"), catalog()).adapter_id  # tr is beta: unvalidated allowed
    with pytest.raises(RoutingError, match="language az"):
        route(replace(AVATAR, language="az-Latn-AZ"), catalog())  # az is unsupported and no engine validates it
    beta_off = dict(catalog().languages)
    beta_off["tr"] = beta_off["tr"].model_copy(update={"support": "production"})
    with pytest.raises(RoutingError, match="unvalidated"):
        route(replace(AVATAR, language="tr"), catalog(languages=beta_off))


def test_resolution_and_duration_limits() -> None:
    decision = route(replace(AVATAR, height=1080), catalog())
    assert decision.adapter_id == "mock_avatar_global"  # the segment mock declares at most 720p
    with pytest.raises(RoutingError, match="duration"):
        route(replace(AVATAR, duration_s=90), catalog())


def test_resolutions_bound_the_short_side_when_the_width_is_known() -> None:
    """ "1080p" is 1080×1920 in portrait: a 1080×1920 portrait frame fits a 1080p engine (Phase 8 fix);
    a 1440×2560 one does not."""
    only_segment = catalog(disabled=frozenset({"mock_avatar_global"}))
    portrait = route(replace(AVATAR, height=1920, width=1080), only_segment)
    assert portrait.adapter_id == "mock_avatar_segment"
    with pytest.raises(RoutingError, match="short side 1440 above 1080"):
        route(replace(AVATAR, height=2560, width=1440), only_segment)
    with pytest.raises(RoutingError, match="height 1920 above 1080"):
        route(replace(AVATAR, height=1920), only_segment)  # without a width the height is compared


def test_production_routing_needs_smoke_validation_unless_sandbox() -> None:
    untested = real_manifest(validation="untested_on_gpu")
    cat = with_manifests(
        untested,
        mock_gpu=False,
        pools=[p.model_copy(update={"enabled": True}) for p in POOLS],
        gpu_providers=frozenset({"runpod_pod"}),
    )
    with pytest.raises(RoutingError, match="validation untested_on_gpu"):
        route(AVATAR, cat)
    sandboxed = real_manifest(status="sandbox", validation="untested_on_gpu")
    cat = with_manifests(
        sandboxed,
        mock_gpu=False,
        pools=[p.model_copy(update={"enabled": True}) for p in POOLS],
        gpu_providers=frozenset({"runpod_pod"}),
    )
    assert route(replace(AVATAR, sandbox=True), cat).adapter_id == "avatar.fake_real"
    with pytest.raises(RoutingError, match="status sandbox"):
        route(AVATAR, cat)


def test_pools_vram_and_license_filters() -> None:
    real = real_manifest()
    cat = with_manifests(real, mock_gpu=False)  # paid pools are disabled; the mock pool runs only mocks
    with pytest.raises(RoutingError, match="no enabled pool"):
        route(AVATAR, cat)
    enabled = [p.model_copy(update={"enabled": True}) for p in POOLS]
    with pytest.raises(RoutingError, match="no enabled pool"):  # enabled, but no RunPod provider is installed
        route(AVATAR, with_manifests(real, mock_gpu=False, pools=enabled, gpu_providers=frozenset({"mock"})))
    runpod = frozenset({"mock", "runpod_pod"})
    assert (
        route(AVATAR, with_manifests(real, mock_gpu=False, pools=enabled, gpu_providers=runpod)).adapter_id
        == "avatar.fake_real"
    )
    huge = real_manifest(runtime={"family": "wan", "requires_gpu": True, "min_vram_gb": 200})
    with pytest.raises(RoutingError, match="no enabled pool"):
        route(AVATAR, with_manifests(huge, mock_gpu=False, pools=enabled, gpu_providers=runpod))
    restricted = real_manifest(
        models=[
            {
                "key": "fake-real",
                "source": {"type": "huggingface", "repo": "x/y", "revision": "abc"},
                "license": {
                    "name": "MiniMax",
                    "url": "u",
                    "commercial_use": True,
                    "conditions": [{"territories_excluded": ["EU"]}],
                    "verified_at": "2026-10-03",
                    "verified_by": "t",
                },
            }
        ]
    )
    with pytest.raises(RoutingError, match="license"):
        route(AVATAR, with_manifests(restricted, mock_gpu=False, pools=enabled, gpu_providers=runpod))


def test_disabled_and_unhealthy_plugins_are_never_routed() -> None:
    with pytest.raises(RoutingError, match="disabled"):
        route(AVATAR, catalog(disabled=frozenset({"mock_avatar_global", "mock_avatar_segment"})))
    decision = route(AVATAR, catalog(unhealthy=frozenset({"mock_avatar_global"})))
    assert decision.adapter_id == "mock_avatar_segment"


FINAL = replace(AVATAR, routing_profile="final")


def test_weights_and_measurements_decide_between_candidates() -> None:
    quality = {("mock_avatar_segment", "avatar.a2v"): 0.95, ("mock_avatar_global", "avatar.a2v"): 0.4}
    assert route(FINAL, catalog(quality=quality)).adapter_id == "mock_avatar_segment"  # final weighs quality
    assert route(AVATAR, catalog(quality=quality)).adapter_id == "mock_avatar_global"  # draft weighs cost and latency
    measured = {
        ("mock_avatar_segment", "gaze"): MeasuredProfile(0.9, 40, "mock"),
        ("mock_avatar_global", "gaze"): MeasuredProfile(0.1, 40, "mock"),
    }
    assert (
        route(replace(FINAL, requested={"gaze": 1.0}), catalog(measured=measured)).adapter_id == "mock_avatar_segment"
    )


def test_mock_sourced_profiles_count_only_with_mock_gpu() -> None:
    measured = {("avatar.fake_real", "emotion_visual"): MeasuredProfile(0.0, 50, "mock")}
    enabled = [p.model_copy(update={"enabled": True}) for p in POOLS]
    cat = with_manifests(
        real_manifest(), mock_gpu=False, pools=enabled, measured=measured, gpu_providers=frozenset({"runpod_pod"})
    )
    decision = route(replace(AVATAR, requested={"emotion_visual": 1.0}), cat)
    assert "measured_behavior=0.500" in decision.reasons  # ignored: a declared, unmeasured control is neutral


def test_a_declared_inability_scores_zero_without_measurements() -> None:
    """`none`/`emergent` controls cannot carry a request: no evidence is needed to score them 0."""
    request = replace(FINAL, requested={"gaze": 1.0})
    decision = route(request, catalog())
    assert decision.adapter_id == "mock_avatar_segment"  # parametric gaze vs emergent gaze
    global_only = route(request, catalog(disabled=frozenset({"mock_avatar_segment"})))
    assert "measured_behavior=0.000" in global_only.reasons


def test_pinned_routes_survive_weight_changes() -> None:
    pinned = route(FINAL, catalog())  # whichever won without quality data
    other = "mock_avatar_segment" if pinned.adapter_id == "mock_avatar_global" else "mock_avatar_global"
    flipped = {(pinned.adapter_id, "avatar.a2v"): 0.0, (other, "avatar.a2v"): 1.0}
    assert route(FINAL, catalog(quality=flipped)).adapter_id == other
    kept = route(replace(FINAL, pinned=pinned), catalog(quality=flipped))
    assert kept.adapter_id == pinned.adapter_id and kept.pinned and kept.same_route(pinned)


def test_a_pinned_route_is_replaced_when_its_adapter_no_longer_passes() -> None:
    pinned = RouteDecision(
        adapter_id="mock_avatar_global", model_id="mock-avatar-global", revision="1", translator_version="0.1.0"
    )
    decision = route(replace(AVATAR, pinned=pinned), catalog(disabled=frozenset({"mock_avatar_global"})))
    assert decision.adapter_id == "mock_avatar_segment" and not decision.pinned
    assert "re-routed" in decision.reason
    stale = pinned.model_copy(update={"revision": "0"})
    assert not route(replace(AVATAR, pinned=stale), catalog()).pinned  # a different revision is a different route


def test_user_pins_win_only_when_they_pass_the_filters() -> None:
    assert route(replace(AVATAR, engine_hint="mock_avatar_segment"), catalog()).adapter_id == "mock_avatar_segment"
    decision = route(replace(AVATAR, engine_hint="mock_avatar_segment", duration_s=45), catalog())
    assert decision.adapter_id == "mock_avatar_global"  # the segment mock renders at most 30 s


def test_fallback_chain_depth_follows_the_profile() -> None:
    profiles = dict(catalog().profiles)
    profiles["draft"] = profiles["draft"].model_copy(update={"fallback_chain_depth": 0})
    assert route(AVATAR, catalog(profiles=profiles)).fallbacks == []


def test_route_digest_ignores_scores_and_reasons() -> None:
    a = route(AVATAR, catalog())
    b = a.model_copy(update={"score": 0.0, "reasons": ["x"], "reason": "y", "fallbacks": [], "pinned": True})
    assert a.digest() == b.digest()


def test_route_preview_applies_capability_language_and_profile_filters_only() -> None:
    assert preview("avatar.a2v", "en", "draft", catalog()) == ["mock_avatar_global", "mock_avatar_segment"]
    assert preview("avatar.a2v", "az", "draft", catalog()) == []
    assert "mock_voice" in preview("voice.tts", "de", "final", catalog())


def test_scoring_is_deterministic() -> None:
    assert route(AVATAR, catalog()) == route(AVATAR, catalog())


def test_registry_overlay_promotes_disables_and_calibrates() -> None:
    """`build_catalog(overlay=…)` (Phase 8; the overlay is `ce_db.registry.RegistryOverlay`,
    duck-typed): a smoke promotion lifts the status/validation filters for a sandbox engine, a
    disabled id never routes, measured profiles load, and only calibrated knobs reach the compiler."""
    from types import SimpleNamespace

    from ce_router import hard_filter

    real = REGISTRY.get("infinitetalk").manifest
    assert (real.status, real.validation) == ("sandbox", "untested_on_gpu")
    request = RouteRequest(capability="avatar.a2v", routing_profile="draft", language="en", height=480, width=832)
    base = catalog(mock_gpu=False)
    assert any(r.startswith("status sandbox") for r in hard_filter(real, request, base))

    overlay = SimpleNamespace(
        status={"infinitetalk": "production"},
        validation={"infinitetalk": "smoke_passed"},
        promotion_basis={"infinitetalk": "smoke"},
        disabled=frozenset({"longcat_avatar"}),
        measured={("infinitetalk", "gaze"): {"success_rate": 0.4, "n": 20, "source": "bench"}},
        knobs={("infinitetalk", "motion_energy"): {"calibrated": True, "monotonic": "true"}},
    )
    promoted = build_catalog(
        REGISTRY, BUNDLE, app_env="test", mock_gpu=False, operator=OperatorProfile(), overlay=overlay
    )
    manifest = promoted.manifests["infinitetalk"]
    assert (manifest.status, manifest.validation) == ("production", "smoke_passed")
    reasons = hard_filter(manifest, request, promoted)
    assert not [r for r in reasons if r.startswith(("status", "validation"))], reasons
    knob = manifest.knobs["motion_energy"]
    assert (knob.calibrated, knob.monotonic) == (True, "true")
    assert "disabled (product decision)" in hard_filter(promoted.manifests["longcat_avatar"], request, promoted)
    assert promoted.measured[("infinitetalk", "gaze")].success_rate == 0.4
    assert real.status == "sandbox"  # the installed manifest is untouched
