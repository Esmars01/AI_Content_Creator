"""I13 — Closed vocabularies for control fields (§4, §13): every control value a plan contains is
in `config/vocab/` at the declared vocab_version; model output is never trusted for control values
(unknown labels go to the repair loop, then to the nearest item with a recorded assumption); stage
outputs reject unknown fields; free text lives only in description/notes fields."""

from __future__ import annotations

from pathlib import Path

import pytest
from ce_core.spec.validate import ValidationContext, validate_spec
from ce_director.models import ActingOut, BriefOut, ScenesOut, StrategyOut
from ce_testing.build import config_bundle
from ce_testing.director import FIXTURES_DIR, plan_fixture
from pydantic import BaseModel, ValidationError

pytestmark = pytest.mark.invariant

SCENARIOS = sorted(p.stem for p in Path(FIXTURES_DIR).glob("*.yaml"))


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_planned_control_value_is_in_the_vocabulary(name: str) -> None:
    spec = plan_fixture(name).spec
    bundle = config_bundle()
    assert spec.vocab_version == bundle.vocab.version
    issues = validate_spec(spec, ValidationContext(vocab=bundle.vocab))
    assert [i for i in issues if i.code == "unknown_vocab"] == []


def test_unknown_labels_are_repaired_or_mapped_and_recorded() -> None:
    outcome = plan_fixture("injection_attempt")
    acting = next(r for r in outcome.runs if r.stage == "acting" and r.provider == "fixture")
    assert acting.status == "failed" and len(acting.attempts) == 3  # repairs exhausted for one label
    assert any("'rage' is not allowed" in p for p in acting.attempts[0]["problems"])
    assert not any("rage" in p for p in acting.attempts[1]["problems"])  # repaired on the second try
    mapped = [a for a in outcome.report.assumptions if "override_mode" in a]
    assert len(mapped) == 1 and "mapped to the nearest item" in mapped[0]
    state = outcome.spec.scenes[0].acting.states[0]  # type: ignore[union-attr]
    assert config_bundle().vocab.has("internal_state", state.internal_state.label)  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (BriefOut, {"input_mode": "idea", "title": "t", "mode": "m", "engine": "x"}),
        (StrategyOut, {"strategy_pack": "p", "hooks": ["h"], "beats": [{"purpose": "hook"}], "cfg_scale": 7}),
        (ScenesOut, {"scenes": [{"segment_keys": ["seg_1"], "purpose": "hook", "seed": 1}]}),
        (ActingOut, {"scenes": []}),
    ],
)
def test_stage_outputs_reject_unknown_fields(model: type[BaseModel], data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_free_text_is_confined_to_description_and_notes_fields() -> None:
    """Every string field of the acting plan and intent that is not a description/notes field is a
    vocabulary token (or a key/reference) in every acceptance plan."""
    vocab = config_bundle().vocab
    free = {"description", "notes", "ref"}
    for name in SCENARIOS:
        spec = plan_fixture(name).spec
        for scene in spec.scenes:
            for field, value in scene.intent.model_dump(mode="json").items():
                if isinstance(value, str) and field not in free:
                    assert vocab.has(f"intent.{field}", value), (name, field, value)
            acting = scene.acting
            assert acting is not None
            assert vocab.has("situation_kind", acting.situation.kind)
            for state in acting.states:
                assert state.internal_state is not None and vocab.has("internal_state", state.internal_state.label)
                for channel, token in state.strategies.model_dump().items():  # type: ignore[union-attr]
                    assert vocab.has(f"strategy.{channel}", token), (name, channel, token)
