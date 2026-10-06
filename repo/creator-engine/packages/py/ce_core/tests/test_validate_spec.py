"""Context-aware spec validation (§11 validators, §15.3 acting validation)."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from ce_core.enums import RecordStatus
from ce_core.errors import Issue
from ce_core.spec.validate import (
    CreatorVersionInfo,
    InMemoryReferences,
    OwnedVersionInfo,
    ValidationContext,
    WorldVersionInfo,
    errors_only,
    validate_spec,
)
from ce_core.spec.videospec import VideoSpec
from ce_core.vocab import Vocabulary, load_vocabulary
from ce_core.yamlio import load_yaml_file
from ce_testing.fixtures import ALEX, alex_creator_dna, example_references, example_spec_dict, home_office_world, words

VOCAB_DIR = Path(__file__).resolve().parents[4] / "config" / "vocab"


@pytest.fixture(scope="module")
def vocab() -> Vocabulary:
    return load_vocabulary(VOCAB_DIR)


def run(vocab: Vocabulary, change: Callable[[dict[str, Any]], None] | None = None, **ctx: Any) -> list[Issue]:
    data = example_spec_dict()
    if change:
        change(data)
    refs = ctx.pop("refs", example_references())
    return validate_spec(VideoSpec.model_validate(data), ValidationContext(vocab=vocab, refs=refs, **ctx))


def codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in errors_only(issues)}


def scene(d: dict[str, Any]) -> dict[str, Any]:
    return d["scenes"][0]  # type: ignore[no-any-return]


def test_example_is_valid_with_and_without_references(vocab: Vocabulary) -> None:
    assert run(vocab) == []
    assert run(vocab, refs=None) == []


@pytest.mark.parametrize(
    ("change", "code", "path_fragment"),
    [
        # vocabulary (I13)
        (
            lambda d: scene(d)["acting"]["states"][0]["emotion"]["displayed"].update(label="jubilant"),
            "unknown_vocab",
            "displayed/label",
        ),
        (
            lambda d: scene(d)["acting"]["states"][0]["strategies"].update(gaze="stare"),
            "unknown_vocab",
            "strategies/gaze",
        ),
        (lambda d: scene(d)["intent"].update(reveal_strategy="surprise_twist"), "unknown_vocab", "reveal_strategy"),
        (lambda d: scene(d)["acting"]["events"][0].update(type="blink_twice"), "unknown_vocab", "events[ev_1]/type"),
        (
            lambda d: d["script"]["segments"][1]["annotations"][0].update(tag="dramatic_pause"),
            "unknown_vocab",
            "an_2]/tag",
        ),
        (
            lambda d: scene(d)["acting"]["states"][0].update(attention_target="ceiling"),
            "unknown_vocab",
            "attention_target",
        ),
        (lambda d: d.update(vocab_version="2025.1.0"), "vocab_version", "/vocab_version"),
        # anchors and tiling
        (lambda d: scene(d)["acting"]["events"][0]["at"].update(word=6), "word_out_of_range", "events[ev_1]/at"),
        (lambda d: scene(d)["shots"][0].update(span=words("seg_1", 0, 7)), "tiling_gap", "/shots"),
        (lambda d: scene(d)["acting"]["states"][1].update(span=words("seg_2", 1, 5)), "tiling_gap", "/states"),
        (
            lambda d: scene(d)["acting"]["states"][0].update(
                span=words("seg_1", 0, 7) | {"end": {"segment_key": "seg_2", "word": 0}}
            ),
            "tiling_overlap",
            "/states",
        ),
        (
            lambda d: scene(d)["shots"][0]["camera"]["moves"][0].update(at={"segment_key": "seg_9", "word": 0}),
            "unknown_segment",
            "moves[mv_1]/at",
        ),
        (
            lambda d: d["script"]["segments"][0]["annotations"][0].update(span=words("seg_2", 0, 0)),
            "annotation_span",
            "an_1]/span",
        ),
        (lambda d: scene(d).update(segment_keys=["seg_1"]), "segment_without_scene", "/script/segments"),
        # acting rules (§15.3)
        (
            lambda d: (
                scene(d)["acting"]["states"][1].update(transition_in=None)
                or scene(d)["acting"]["states"][1]["emotion"]["displayed"].update(intensity=0.1)
                or scene(d)["acting"]["states"][1]["emotion"]["felt"].update(intensity=0.1)
            ),
            "intensity_jump",
            "transition_in",
        ),
        (lambda d: scene(d)["acting"]["events"][1].update(direction="left"), "event_param", "events[ev_2]/direction"),
        (
            lambda d: scene(d)["acting"]["events"][0].update(
                type="finger_point_at_camera", direction=None, purpose="emphasis"
            ),
            "avoidance",
            "events[ev_1]/type",
        ),
        (
            lambda d: scene(d)["acting"]["states"][0]["emotion"]["displayed"].update(intensity=0.95),
            "outside_dna_range",
            "displayed",
        ),
        (
            lambda d: scene(d)["acting"]["states"][0]["strategies"].update(posture="standing_upright"),
            "posture_not_allowed",
            "strategies/posture",
        ),
        (
            lambda d: d["intent"]["video"].update(emotional_arc=["serious", "confident"]),
            "emotional_arc",
            "emotional_arc",
        ),
        # world binding (§19.3)
        (
            lambda d: scene(d)["world"].update(camera_position_key="cam_ceiling"),
            "unknown_camera_position",
            "camera_position_key",
        ),
        (lambda d: scene(d)["world"].update(time_of_day="night"), "time_not_allowed", "time_of_day"),
        (
            lambda d: scene(d)["world"]["overrides"]["element_states"].update(el_monitor="dim"),
            "unknown_element_state",
            "element_states",
        ),
        (
            lambda d: scene(d)["world"]["overrides"].update(
                move_elements=[{"key": "el_desk", "position": [0.1, 0.1, 0]}]
            ),
            "element_not_movable",
            "move_elements",
        ),
        (
            lambda d: scene(d)["world"]["overrides"].update(lighting={"color_temp_k_delta": 900}),
            "lighting_out_of_tolerance",
            "lighting",
        ),
        (
            lambda d: scene(d)["shots"][0]["camera"].update(profile_id="dslr"),
            "camera_profile_not_allowed",
            "profile_id",
        ),
        (lambda d: scene(d)["cast"][0].update(placement="zone_sofa"), "unknown_zone", "placement"),
        # structure, flags, locks, references inside the spec
        (lambda d: scene(d)["shots"][0].update(type="two_shot"), "two_shot_disabled", "/type"),
        (
            lambda d: d["locks"].append({"group": "voice", "scope": {"scene_keys": ["scn_hook"]}, "set_by": "user"}),
            "lock_scope",
            "scene_keys",
        ),
        (
            lambda d: d["locks"].append(
                {"group": "acting", "scope": {"scene_keys": ["scn_missing"]}, "set_by": "user"}
            ),
            "lock_scope",
            "scene_keys",
        ),
        (
            lambda d: d["locks"].append({"group": "mood", "scope": {}, "set_by": "user"}),
            "unknown_vocab",
            "/locks/1/group",
        ),
        (
            lambda d: scene(d)["shots"][1]["derived_from"][0].update(ref="/scenes[scn_hook]/acting/events[ev_9]"),
            "dangling_ref",
            "derived_from",
        ),
        (lambda d: d["memory"].update(snapshots=[]), "memory_pin", "/memory/snapshots"),
        (lambda d: d["meta"].update(target_duration_s=30), "duration", "target_duration_s"),
        (
            lambda d: d["script"]["segments"][0].update(
                quote_source={"consent_id": str(ALEX.ORG_ID), "asset_id": str(ALEX.CANONICAL_FACE_ASSET_ID)}
            ),
            "quote_consent",
            "quote_source",
        ),
        (lambda d: d["script"]["segments"][0].update(speaker_key="char_sam"), "unknown_character", "speaker_key"),
    ],
)
def test_each_rule_reports_an_addressed_issue(
    vocab: Vocabulary, change: Callable[[dict[str, Any]], None], code: str, path_fragment: str
) -> None:
    issues = run(vocab, change)
    matching = [i for i in errors_only(issues) if i.code == code]
    assert matching, f"expected {code}, got {[(i.code, i.path) for i in issues]}"
    assert any(path_fragment in (i.path or "") for i in matching), [i.path for i in matching]


def test_out_of_character_downgrades_range_violation_to_warning(vocab: Vocabulary) -> None:
    def change(d: dict[str, Any]) -> None:
        state = scene(d)["acting"]["states"][0]
        state["emotion"]["displayed"]["intensity"] = 0.95
        state["out_of_character"] = {"allowed": True, "reason": "parody of an overexcited ad"}

    issues = run(vocab, change)
    assert codes(issues) == set()
    assert [i.code for i in issues] == ["out_of_character"]


def test_reference_rules(vocab: Vocabulary) -> None:
    refs = example_references()
    other_creator = UUID("0192f0a0-0000-7000-8000-0000000000ee")
    refs.wardrobe_versions[ALEX.WARDROBE_VERSION_ID] = OwnedVersionInfo(
        ALEX.WARDROBE_VERSION_ID, RecordStatus.APPROVED, other_creator
    )
    refs.creator_versions[ALEX.CREATOR_VERSION_ID] = CreatorVersionInfo(
        ALEX.CREATOR_VERSION_ID, ALEX.CREATOR_ID, RecordStatus.DRAFT, alex_creator_dna()
    )
    found = codes(run(vocab, refs=refs))
    assert {"foreign_override", "not_approved"} <= found

    missing = InMemoryReferences()
    assert "unknown_reference" in codes(run(vocab, refs=missing))


def test_draft_world_only_as_a_proposal(vocab: Vocabulary) -> None:
    refs = example_references()
    refs.world_versions[ALEX.WORLD_VERSION_ID] = WorldVersionInfo(
        ALEX.WORLD_VERSION_ID, RecordStatus.DRAFT, home_office_world()
    )
    assert "not_approved" in codes(run(vocab, refs=refs))
    assert run(vocab, refs=refs, allow_draft_world_ids=frozenset({ALEX.WORLD_VERSION_ID})) == []


def test_two_shot_allowed_when_multi_character_enabled(vocab: Vocabulary) -> None:
    issues = run(vocab, lambda d: scene(d)["shots"][0].update(type="two_shot"), multi_character_enabled=True)
    assert "two_shot_disabled" not in codes(issues)


def test_same_dimension_event_and_annotation_on_one_span(tmp_path: Path) -> None:
    """With a vocabulary where an event shares a dimension with an annotation type, duplicates are rejected (§10.6)."""
    target = tmp_path / "vocab"
    shutil.copytree(VOCAB_DIR, target)
    events = load_yaml_file(target / "behavior_events.yaml")
    events["event_types"]["audible_pause"] = {"dimension": "prosody_pause", "default_duration_ms": 300, "params": []}
    (target / "behavior_events.yaml").write_text(yaml.safe_dump(events, sort_keys=False), encoding="utf-8")
    descriptions = load_yaml_file(target / "descriptions.yaml")
    descriptions["descriptions"]["event_type"]["audible_pause"] = "a pause the face shows"
    (target / "descriptions.yaml").write_text(
        yaml.safe_dump(descriptions, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    custom = load_vocabulary(target)
    assert custom.lint() == []

    def change(d: dict[str, Any]) -> None:
        scene(d)["acting"]["events"].append(
            {
                "key": "ev_3",
                "character_key": "char_alex",
                "type": "audible_pause",
                "at": {"segment_key": "seg_2", "word": 3},
                "priority": "nice",
            }
        )

    assert "same_dimension_duplicate" in codes(run(custom, change))
