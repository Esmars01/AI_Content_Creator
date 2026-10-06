"""Director units: the repair loop and stage fallbacks, the template Director, vocabulary mapping,
exact-script segmentation, timing, prompts and run records."""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from ce_contracts.models import LLMRequest, LLMResult
from ce_director import Director, PlanRequest
from ce_director.models import ScriptOut
from ce_director.template import distribute, template_brief
from ce_director.vocabmap import ControlField, VocabMapper
from ce_llm import DATA_RULE, LLMProvider, create_llm_provider
from ce_testing.build import config_bundle
from ce_testing.director import FIXTURES_DIR, director_context, director_deps, fixture_inputs, plan_fixture
from ce_testing.fixtures import ALEX
from ce_voice import ExactScriptError

STAGES = ("interpret", "strategy", "script", "fact_check", "scenes", "acting")


class Capturing(LLMProvider):
    key = "capturing"

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.model = inner.model
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)
        return await self.inner.complete(request)


def fixture_provider(path: Path) -> LLMProvider:
    return create_llm_provider("fixture", app_env="test", config={"fixtures_dir": str(path)})


def plan(request: PlanRequest, provider: LLMProvider | None = None, **ctx: Any) -> Any:
    deps = director_deps(provider) if provider is not None else director_deps()
    return asyncio.run(Director(deps).plan(request, director_context(**ctx)))


def write_variant(tmp_path: Path, name: str, **responses: Any) -> str:
    """A copy of fixture `name` with some stage responses replaced; returns its input."""
    doc = yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    doc["responses"].update(responses)
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(doc["inputs"][0])


# ---------------------------------------------------------------------- prompts and runs


def test_every_llm_stage_renders_its_prompt_with_the_data_rule() -> None:
    capture = Capturing(fixture_provider(FIXTURES_DIR))
    outcome = plan(PlanRequest(input=fixture_inputs("ugc_product_review")[0]), capture)
    stages = [r.stage for r in capture.requests]
    assert stages[: len(STAGES)] == list(STAGES) and "testimonial_guard" in stages
    for request in capture.requests:
        system = next(m.content for m in request.messages if m.role == "system")
        assert DATA_RULE in system
        assert request.json_schema and request.scenario_id
    rows = [r.row() for r in outcome.runs]
    assert all(r["status"] in ("succeeded", "repaired", "failed") for r in rows)
    llm_rows = [r for r in rows if r["provider"] == "capturing"]
    assert {r["template_version"].split("/")[0] for r in llm_rows} >= set(STAGES)
    acting = next(r for r in outcome.runs if r.stage == "acting")
    assert set(acting.input["route_preview"]) == {"avatar.a2v", "voice.tts"}  # recorded in director_runs
    assert {"mock_avatar_global", "mock_avatar_segment"} <= set(acting.input["route_preview"]["avatar.a2v"])
    assert "mock_voice" in acting.input["route_preview"]["voice.tts"]


def test_invalid_json_is_repaired_and_every_attempt_is_kept() -> None:
    outcome = plan_fixture("explain_ai_agents")
    run = next(r for r in outcome.runs if r.stage == "script")
    assert run.status == "repaired" and len(run.attempts) == 2
    assert run.attempts[0]["output"] is None and run.attempts[0]["problems"]
    assert run.row()["output"]["attempts"][0]["raw"].startswith("Sure!")


def test_a_stage_that_stays_invalid_falls_back_to_its_template(tmp_path: Path) -> None:
    broken = {"scenes": [{"segment_keys": ["seg_9"], "purpose": "hook"}]}
    text = write_variant(tmp_path, "explain_ai_agents", scenes=[broken, broken, broken])
    outcome = plan(PlanRequest(input=text), fixture_provider(tmp_path))
    statuses = [(r.stage, r.status, r.provider) for r in outcome.runs if r.stage == "scenes"]
    assert statuses == [("scenes", "failed", "fixture"), ("scenes", "succeeded", "template")]
    assert any("scenes: the model's output still failed validation" in a for a in outcome.report.assumptions)
    assert outcome.planner == "llm"  # one template stage inside an LLM plan


def test_a_fixture_miss_plans_with_the_labeled_template_director(tmp_path: Path) -> None:
    outcome = plan(
        PlanRequest(input="Make a 20-second video about houseplants. Water them less."), fixture_provider(tmp_path)
    )
    assert outcome.planner == "template" and outcome.report.planner == "template"
    assert any("template Director" in a for a in outcome.report.assumptions)
    assert [s.text for s in outcome.spec.script.segments] == [
        "Make a 20-second video about houseplants.",
        "Water them less.",
    ]
    assert {r.provider for r in outcome.runs if r.stage in STAGES} == {"template"}


def test_template_director_is_deterministic_and_respects_request_constraints() -> None:
    request = PlanRequest(
        input="Explain compound interest. It grows on itself. Start early.", mode="educational", target_duration_s=12
    )
    deps = director_deps()
    deps.provider = None
    deps.ids = lambda: UUID(int=7)  # snapshot ids are the only allocated values
    a = asyncio.run(Director(deps).plan(request, director_context()))
    b = asyncio.run(Director(deps).plan(request, director_context()))
    assert a.spec.model_dump(mode="json") == b.spec.model_dump(mode="json") and a.planner == "template"
    assert a.spec.meta.mode == "educational"
    brief = template_brief(PlanRequest(input="Some idea", input_mode="exact_script"), config_bundle())
    assert brief.input_mode == "exact_script" and brief.script_span is not None
    assert distribute(5, 5) == [0, 1, 2, 3, 4] and distribute(2, 5) == [0, 4] and distribute(7, 3)[-1] == 2


def test_request_fields_are_constraints_over_the_models_reading() -> None:
    outcome = plan(
        PlanRequest(
            input=fixture_inputs("explain_ai_agents")[0], target_duration_s=28, platform_targets=["youtube_shorts"]
        )
    )
    assert outcome.spec.meta.target_duration_s == 28
    assert outcome.spec.render.outputs[0].preset_id == "youtube_shorts_1080x1920_30"


def test_an_explicit_cast_member_is_used() -> None:
    from ce_director.models import CastRequest

    outcome = plan(
        PlanRequest(input=fixture_inputs("calm_but_surprised_she")[0], cast=[CastRequest(creator_id=ALEX.CREATOR_ID)])
    )
    assert outcome.spec.cast[0].creator_version_id == ALEX.CREATOR_VERSION_ID


# ---------------------------------------------------------------------- exact scripts


def test_exact_boundaries_that_cut_words_are_repaired(tmp_path: Path) -> None:
    doc = yaml.safe_load((FIXTURES_DIR / "exact_desk_45s.yaml").read_text(encoding="utf-8"))
    good = doc["responses"]["script"]
    bad = copy.deepcopy(good)
    bad["boundaries"][0] += 2  # inside "The"
    text = write_variant(tmp_path, "exact_desk_45s", script=[bad, good])
    outcome = plan(PlanRequest(input=text), fixture_provider(tmp_path))
    run = next(r for r in outcome.runs if r.stage == "script")
    assert run.status == "repaired" and any("cuts a word" in p for p in run.attempts[0]["problems"])


def test_exact_segments_reject_text_outside_any_segment() -> None:
    director = Director(director_deps())
    raw = "Intro: Hello there. General Kenobi."
    from ce_director.models import BriefOut

    brief = BriefOut.model_validate(
        {
            "input_mode": "exact_script",
            "title": "t",
            "mode": "talking_head_explainer",
            "script_span": {"start": 7, "end": len(raw)},
        }
    )
    segments = director._exact_segments(brief, raw, [20])
    assert [s.text for s in segments] == ["Hello there.", "General Kenobi."]
    with pytest.raises(ExactScriptError):
        director._exact_segments(brief, raw, [2])
    assert ScriptOut(boundaries=[20]).boundaries == [20]


# ---------------------------------------------------------------------- vocabulary mapping (I13)


def test_unknown_labels_are_listed_for_repair_then_mapped_to_the_nearest_item() -> None:
    mapper = VocabMapper(config_bundle().vocab)
    fields = [ControlField("states[].label", "emotion", optional=False)]
    data = {"states": [{"label": "furious"}, {"label": "confident"}, {"label": "teleporting"}]}
    problems = mapper.problems(data, fields)
    assert len(problems) == 2 and "use one of" in problems[0]
    notes: list[str] = []
    mapper.coerce(data, fields, notes.append, stage="acting")
    assert data["states"][0]["label"] == "angry"  # alias in descriptions.yaml
    assert data["states"][1]["label"] == "confident"
    assert mapper.known("emotion", data["states"][2]["label"]) and len(notes) == 2
    assert mapper.nearest("strategy.gaze", "Look Down Thinking") == "look_down_thinking"
