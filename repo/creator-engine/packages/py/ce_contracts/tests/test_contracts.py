"""ce_contracts (§23, §24): capability catalog, manifest validation and loader refusals, license
closure, and the sync between ce_core's CompiledBehavior and the 3.10-compatible wire form."""

from __future__ import annotations

from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from ce_core.behavior.compiled import EditorialAction, ProsodyPlan, Realization, VisualSubSpan
from ce_core.enums import CoverageLevel, RealizationMethod
from ce_core.vocab import load_vocabulary

from ce_contracts import CAPABILITIES, INTERFACE_ONLY, ContractModel, capability, discover, license_closure
from ce_contracts.behavior import (
    BehaviorMatrix,
    DirectiveRealization,
    DirectiveSubSpan,
    EditorialDirective,
    ProsodyDirectives,
)
from ce_contracts.plugins import load_manifest_text

ROOT = Path(__file__).resolve().parents[4]
SEGMENT_MANIFEST = ROOT / "plugins/mock/src/ce_plugins_mock/avatar_segment/plugin.yaml"


def _manifest(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(SEGMENT_MANIFEST.read_text(encoding="utf-8"))
    data.update(changes)
    return data


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    path = folder / "plugin.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _rejections(tmp_path: Path, *manifests: dict[str, Any], **options: Any) -> list[str]:
    paths = [_write(tmp_path, f"p{i}", m) for i, m in enumerate(manifests)]
    registry = discover(app_env="test", include_mocks=True, extra_manifests=paths, **options)
    return [r.reason for r in registry.rejected if r.source in {str(p) for p in paths}]


def test_every_capability_has_typed_request_and_result_models() -> None:
    assert len(CAPABILITIES) >= 40 and set(CAPABILITIES) >= INTERFACE_ONLY
    for cap in CAPABILITIES.values():
        assert issubclass(cap.request, ContractModel) and issubclass(cap.result, ContractModel), cap.id
    assert capability("avatar.a2v").behavior_capable and capability("voice.tts").behavior_capable
    with pytest.raises(KeyError, match="unknown capability"):
        capability("avatar.teleport")


def test_installed_manifests_use_only_vocabulary_dimensions() -> None:
    dims = load_vocabulary(ROOT / "config" / "vocab").dimensions
    registry = discover(app_env=None, include_mocks=True, dimension_names=list(dims))
    assert registry.rejected == []
    assert {"mock_avatar_global", "mock_avatar_segment", "mock_identity_trainer"} <= set(registry.plugins)


def test_the_loader_refuses_invalid_manifests_with_a_reason(tmp_path: Path) -> None:
    good = _manifest(id="probe_ok")
    no_license = _manifest(id="probe_nolicense")
    del no_license["models"][0]["license"]
    unlicensed_dep = _manifest(id="probe_dep")
    unlicensed_dep["models"][0]["dependencies"] = [{"ref": "org/vae@abc"}]
    bad_capability = _manifest(id="probe_cap", capabilities=[{"id": "avatar.teleport"}])
    no_translator = _manifest(id="probe_tr", behavior_translator=None)
    ability_as_feature = _manifest(id="probe_feat")
    ability_as_feature["capabilities"][0]["features"] = ["i2v", "gaze_control"]
    reasons = _rejections(tmp_path, good, no_license, unlicensed_dep, bad_capability, no_translator, ability_as_feature)
    assert len(reasons) == 5, reasons
    joined = "\n".join(reasons)
    assert "has no license block" in joined and "dependency org/vae@abc" in joined
    assert "unknown capability 'avatar.teleport'" in joined
    assert "ship a BehaviorTranslator" in joined


def test_unknown_dimensions_and_duplicate_ids_are_refused(tmp_path: Path) -> None:
    odd = _manifest(id="probe_dims")
    odd["behavior_matrix"]["telepathy"] = {"control": "parametric", "temporal_precision": "word"}
    dims = list(load_vocabulary(ROOT / "config" / "vocab").dimensions)
    (reason,) = _rejections(tmp_path, odd, dimension_names=dims)
    assert "unknown dimensions" in reason and "telepathy" in reason
    twin_dir = tmp_path / "twin"
    twin_dir.mkdir()
    (reason,) = _rejections(twin_dir, _manifest())  # same id as the installed mock_avatar_segment
    assert "mock_avatar_segment" in reason


def test_mocks_environments_and_families_filter_without_rejecting(tmp_path: Path) -> None:
    path = _write(tmp_path, "probe", _manifest(id="probe_filter"))
    assert "probe_filter" not in discover(app_env="test", include_mocks=False, extra_manifests=[path]).plugins
    assert "probe_filter" not in discover(app_env="prod", include_mocks=True, extra_manifests=[path]).plugins
    assert (
        "probe_filter"
        not in discover(app_env="test", include_mocks=True, families=["wan"], extra_manifests=[path]).plugins
    )
    kept = discover(app_env="test", include_mocks=True, families=["cpu_model"], extra_manifests=[path])
    assert "probe_filter" in kept.plugins and kept.rejected == []


def test_license_closure_lists_the_model_and_its_dependencies() -> None:
    data = _manifest(id="probe_closure")
    licence = dict(data["models"][0]["license"])
    data["models"][0]["dependencies"] = [{"ref": "org/vae@abc", "license": {**licence, "name": "Apache-2.0"}}]
    manifest = load_manifest_text(yaml.safe_dump(data))
    closure = license_closure(manifest)
    assert [ref for ref, _ in closure] == ["mock-avatar-segment@1", "org/vae@abc"]
    assert closure[1][1].name == "Apache-2.0"
    assert license_closure(manifest, "no-such-model") == []


def test_behavior_matrix_validation() -> None:
    with pytest.raises(ValueError, match="cannot declare temporal precision"):
        BehaviorMatrix.model_validate({"gaze": {"control": "emergent", "temporal_precision": "word"}})
    with pytest.raises(ValueError, match="recommended_chunk_s"):
        BehaviorMatrix.model_validate({"temporal_control": {"max_clip_s": 5, "recommended_chunk_s": 10}})
    matrix = BehaviorMatrix.model_validate({"gaze": {"control": "parametric", "temporal_precision": "word"}})
    assert matrix.control("gaze").control == "parametric" and matrix.control("gesture").control == "none"


def test_wire_directives_stay_in_sync_with_compiled_behavior() -> None:
    """ce_contracts cannot import ce_core (Python 3.10, §7); the wire form must accept what the
    compiler produces, with word anchors resolved to seconds (ADR 0032)."""
    assert set(get_args(DirectiveRealization.model_fields["level"].annotation)) == {c.value for c in CoverageLevel}
    realization = Realization(
        item_ref="/scenes[scn_1]/acting/states[st_1]/emotion",
        dimension="emotion_visual",
        level=CoverageLevel.HONORED,
        method=RealizationMethod.NATIVE_SEGMENT,
        detail="x",
    )
    assert DirectiveRealization.model_validate(realization.model_dump(mode="json")).method == "native_segment"
    plan = ProsodyPlan(
        character_key="char_alex",
        segment_key="seg_1",
        strategy="slow_measured",
        emotion="serious",
        emotion_intensity=0.6,
        rate=0.9,
        energy=0.4,
        pitch_variation=0.3,
        emphasis_words=[2],
        pauses=[{"after_word": 3, "ms": 300}],  # type: ignore[list-item]
    )
    wire = ProsodyDirectives.model_validate(plan.model_dump(mode="json"))
    assert wire.model_dump() == ProsodyDirectives.model_validate(wire.model_dump()).model_dump()
    # wire-only: `descriptions` (to_directives adds the vocabulary text of the labels, Phase 8, ADR 0051) and
    # `pitch_semitones` (compile_voice applies the cast member's voice_prosody offset, audit OUT-PROSODY)
    assert set(ProsodyPlan.model_fields) == set(ProsodyDirectives.model_fields) - {"descriptions", "pitch_semitones"}
    anchored = set(VisualSubSpan.model_fields) - {"span", "knobs"}  # span → start_s/end_s; knobs → floats
    assert anchored <= set(DirectiveSubSpan.model_fields)
    assert set(get_args(EditorialAction.model_fields["kind"].annotation)) and {"kind", "item_ref", "detail"} <= set(
        EditorialDirective.model_fields
    )
