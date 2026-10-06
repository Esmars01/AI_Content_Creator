"""CBS, CompiledBehavior, ObservedBehavior, coverage outcomes and the BuildManifest (§12.3, §15–§16)."""

from __future__ import annotations

import json
import re
from itertools import product
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from ce_core.behavior.cbs import CanonicalBehaviorSpec, CBSContent
from ce_core.behavior.compiled import CompiledBehavior
from ce_core.behavior.coverage import (
    BehaviorCoverageReport,
    CoverageEntry,
    display_summary,
    is_delivered,
    outcome_of,
)
from ce_core.behavior.observed import ObservedBehavior
from ce_core.build import BuildManifest, ManifestEntry, RouteDecision
from ce_core.enums import CoverageLevel, ObservationVerdict, Outcome
from ce_testing.fixtures import route_digest_placeholder
from pydantic import ValidationError

SPEC_DOC = Path(__file__).resolve().parents[4] / "docs" / "MASTER_BUILD_PROMPT.md"


def doc_block(predicate: Any) -> dict[str, Any]:
    text = SPEC_DOC.read_text(encoding="utf-8").replace('"sha256:…"', json.dumps(route_digest_placeholder()))
    blocks = [json.loads(m.group(1)) for m in re.finditer(r"```json\n(.*?)\n```", text, re.S)]
    [block] = [b for b in blocks if predicate(b)]
    return block


def test_the_prompt_cbs_example_parses_and_digests() -> None:
    doc = doc_block(lambda b: "envelope" in b)
    content = CBSContent.model_validate(doc["content"])
    doc["envelope"]["content_digest"] = content.digest()
    cbs = CanonicalBehaviorSpec.model_validate(doc)
    assert len(cbs.content.requested_controls) == 11
    assert {r.dimension for r in cbs.content.requested_controls} >= {"gaze", "emotion_visual", "prosody_pause"}


def test_cbs_digest_mismatch_is_rejected() -> None:
    doc = doc_block(lambda b: "envelope" in b)
    with pytest.raises(ValidationError, match="content_digest"):
        CanonicalBehaviorSpec.model_validate(doc)  # the placeholder digest does not match


def test_cbs_content_digest_is_independent_of_envelope() -> None:
    doc = doc_block(lambda b: "envelope" in b)
    content = CBSContent.model_validate(doc["content"])
    doc["envelope"]["content_digest"] = content.digest()
    a = CanonicalBehaviorSpec.model_validate(doc)
    doc["envelope"]["scope"]["version_id"] = "0192f0a0-0000-7000-8000-0000000000ff"
    b = CanonicalBehaviorSpec.model_validate(doc)
    assert a.content.digest() == b.content.digest()


def test_cbs_rejects_characters_outside_its_cast() -> None:
    doc = doc_block(lambda b: "envelope" in b)
    doc["content"]["events"][0]["character_key"] = "char_sam"
    with pytest.raises(ValidationError, match="not in the CBS cast"):
        CBSContent.model_validate(doc["content"])


def test_coverage_entry_example_from_the_prompt() -> None:
    entry = CoverageEntry.model_validate(doc_block(lambda b: "approximation_executed" in b))
    assert entry.outcome == Outcome.APPROXIMATED_NOT_OBSERVED
    assert entry.expected_for_method is True
    assert display_summary(entry.compiled.level, entry.outcome, entry.expected_for_method).startswith(
        "APPROXIMATED → FAILED OBSERVATION (expected"
    )


def test_exactly_twelve_outcomes() -> None:
    produced = {outcome_of(level, verdict) for level, verdict in product(CoverageLevel, ObservationVerdict)}
    assert produced == set(Outcome) and len(Outcome) == 12
    assert outcome_of(CoverageLevel.HONORED, None) is None
    assert outcome_of(CoverageLevel.UNSUPPORTED, ObservationVerdict.CONFIRMED) == Outcome.UNSUPPORTED_OBSERVED
    assert outcome_of(CoverageLevel.UNSUPPORTED, ObservationVerdict.NOT_OBSERVED) == Outcome.UNSUPPORTED


def test_only_confirmed_counts_as_delivered() -> None:
    delivered = {o for o in Outcome if is_delivered(o)}
    assert delivered == {Outcome.HONORED_CONFIRMED, Outcome.APPROXIMATED_CONFIRMED}


def coverage_entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_ref": "/scenes[scn_hook]/acting/events[ev_2]",
        "character_key": "char_alex",
        "dimension": "facial_expression",
        "requested": "small smile",
        "compiled": {"level": "HONORED", "method": "native_parametric"},
        "observed": {"verdict": "CONFIRMED", "measures": {"smile": 0.5}, "confidence": 0.8},
        "outcome": "HONORED_CONFIRMED",
        "expected_for_method": False,
    }
    base.update(overrides)
    return base


def test_coverage_entry_consistency_rules() -> None:
    CoverageEntry.model_validate(coverage_entry())
    with pytest.raises(ValidationError, match="does not follow"):
        CoverageEntry.model_validate(coverage_entry(outcome="HONORED_PARTIAL"))
    with pytest.raises(ValidationError, match="editorial"):
        CoverageEntry.model_validate(coverage_entry(expected_for_method=True))


def test_report_summary_counts() -> None:
    report = BehaviorCoverageReport.model_validate(
        {
            "stage": "observed",
            "entries": [coverage_entry(), coverage_entry(item_ref="/scenes[scn_hook]/acting/events[ev_1]")],
        }
    )
    assert report.summary_counts() == {"delivered": 2, "level:HONORED": 2, "outcome:HONORED_CONFIRMED": 2}


def test_compiled_and_observed_models_round_trip() -> None:
    compiled = CompiledBehavior.model_validate(
        {
            "node_kind": "behavior.compile_voice",
            "pass": "build_time",
            "cbs_content_digest": route_digest_placeholder(),
            "target_key": "seg_2",
            "realizations": [
                {
                    "item_ref": "/scenes[scn_hook]/acting/states[st_2]/strategies/prosody",
                    "dimension": "prosody_rate",
                    "level": "HONORED",
                    "method": "native_parametric",
                }
            ],
            "prosody_plans": [
                {
                    "character_key": "char_alex",
                    "segment_key": "seg_2",
                    "strategy": "slow_measured",
                    "rate": 0.9,
                    "energy": 0.55,
                    "pitch_variation": 0.35,
                    "pauses": [{"after_word": 3, "ms": 350}],
                }
            ],
        }
    )
    assert CompiledBehavior.model_validate_json(compiled.model_dump_json(by_alias=True)) == compiled
    observed = ObservedBehavior.model_validate(
        {
            "take_artifact_id": "0192f0a0-0000-7000-8000-0000000000d1",
            "analyzers": [{"capability": "face.landmarks", "adapter_id": "mock_observer", "revision": "0"}],
            "tracks": [
                {"character_key": "char_alex", "series": {"smile": [0.1, 0.5, 0.2]}, "face_detected_ratio": 1.0}
            ],
        }
    )
    assert observed.tracks[0].series["smile"][1] == 0.5


def test_manifest_is_insert_only() -> None:
    route = RouteDecision(adapter_id="mock_avatar_global", model_id="mock", revision="0")
    entries = [
        ManifestEntry(node_key="avatar.render:sht_1:t1", kind="route", route=route),
        ManifestEntry(node_key="avatar.render:sht_1:t1", kind="seed", seed=42),
        ManifestEntry(node_key="avatar.render:sht_1:t1", kind="artifact", artifact_id=str(UUID(int=1))),
    ]
    manifest = BuildManifest.assemble(entries)
    assert manifest.effective_seeds == {"avatar.render:sht_1:t1": 42}
    with pytest.raises(ValueError, match="insert-only"):
        BuildManifest.assemble([*entries, ManifestEntry(node_key="avatar.render:sht_1:t1", kind="seed", seed=7)])
    assert (
        route.digest()
        == RouteDecision(adapter_id="mock_avatar_global", model_id="mock", revision="0", reason="x").digest()
    )
