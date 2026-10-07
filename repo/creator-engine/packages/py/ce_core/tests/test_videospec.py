"""VideoSpec model (§11): the prompt's example parses, round-trips, and structural rules hold."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
from ce_core.canonical import content_digest
from ce_core.spec.videospec import VideoSpec
from ce_testing.fixtures import example_spec, example_spec_dict, route_digest_placeholder
from pydantic import ValidationError


def _spec_doc() -> Path:
    from ce_testing import docs

    return docs.require_spec()


def doc_json_blocks() -> list[dict[str, Any]]:
    text = _spec_doc().read_text(encoding="utf-8").replace('"sha256:…"', json.dumps(route_digest_placeholder()))
    return [json.loads(m.group(1)) for m in re.finditer(r"```json\n(.*?)\n```", text, re.S)]


def test_the_prompt_example_parses_as_written() -> None:
    """The abbreviated §11 example (digest placeholders filled) is a valid VideoSpec model."""
    [doc_spec] = [b for b in doc_json_blocks() if b.get("schema_version") == "1.0"]
    spec = VideoSpec.model_validate(doc_spec)
    assert spec.scenes[0].acting is not None
    assert [s.key for s in spec.scenes[0].acting.states] == ["st_1", "st_2"]


def test_round_trip_is_lossless() -> None:
    spec = example_spec()
    again = VideoSpec.model_validate_json(spec.model_dump_json())
    assert again == spec
    assert again.model_dump(mode="json") == spec.model_dump(mode="json")


def test_content_digest_ignores_identity_fields() -> None:
    a = example_spec_dict()
    b = copy.deepcopy(a)
    b["version_id"] = "0192f0a0-0000-7000-8000-0000000000ff"
    b["parent_version_id"] = a["version_id"]
    assert VideoSpec.model_validate(a).content_digest() == VideoSpec.model_validate(b).content_digest()
    b["meta"]["title"] = "Another title"
    assert VideoSpec.model_validate(a).content_digest() != VideoSpec.model_validate(b).content_digest()


def test_content_digest_is_the_digest_of_content_dict() -> None:
    spec = example_spec()
    assert spec.content_digest() == content_digest(spec.content_dict())
    assert "video_id" not in spec.content_dict()


def test_specs_are_immutable() -> None:
    spec = example_spec()
    with pytest.raises(ValidationError):
        spec.meta.title = "changed"  # type: ignore[misc]


def test_wording_locked_is_computed_and_read_only() -> None:
    data = example_spec_dict()
    assert VideoSpec.model_validate(data).to_api()["script"]["wording_locked"] is False
    data["locks"].append({"group": "script", "scope": {}, "set_by": "user"})
    assert VideoSpec.model_validate(data).wording_locked is True
    bad = example_spec_dict()
    bad["script"]["wording_locked"] = True
    with pytest.raises(ValidationError, match="read-only"):
        VideoSpec.model_validate(bad)


def mutate(change: Any) -> dict[str, Any]:
    data = example_spec_dict()
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["scenes"][0]["shots"][1].update(key="sht_1"), "duplicate keys"),
        (lambda d: d["scenes"][0]["acting"]["states"][0].update(source="memory"), "Input should be"),
        (lambda d: d["scenes"][0]["acting"]["states"][1]["emotion"].update(masking=True), "masking requires"),
        (
            lambda d: d["scenes"][0]["acting"]["events"][0].update(span=d["scenes"][0]["acting"]["states"][0]["span"]),
            "exactly one",
        ),
        (lambda d: d["scenes"][0]["shots"][1].update(broll=None), "needs `broll`"),
        (lambda d: d["scenes"][0]["shots"][0].update(character_key=None), "needs a character_key"),
        (lambda d: d["scenes"][0]["shots"][0]["takes"].update(selected_take_key="tk_3"), "exceeds takes.count"),
        (lambda d: d["scenes"][0]["shots"][1]["derived_from"][0].update(route_digest=None), "route_digest"),
        (
            lambda d: d["scenes"][0]["shots"][0]["camera"]["moves"][0]["derived_from"][0].update(
                ref="/scenes[0]/intent"
            ),
            "array indices",
        ),
        (lambda d: d["meta"].update(language="english"), "BCP-47"),
        (lambda d: d["cast"][0].update(key="alex"), "invalid character key"),
        (
            lambda d: d["scenes"][0]["world"]["overrides"].update(hide_elements=["el_monitor"]),
            "both hidden and changed",
        ),
        (lambda d: d["brief"].update(selected_hook_key="hk_9"), "not a hook candidate"),
        (lambda d: d["scenes"][0].update(unknown_field=1), "Extra inputs are not permitted"),
        (lambda d: d.update(cast=[]), "at least 1"),
        (
            lambda d: d["script"]["segments"][0]["annotations"][0].update(type="pronunciation", tag="respell"),
            "respelling",
        ),
    ],
)
def test_structural_rules(change: Any, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        VideoSpec.model_validate(mutate(change))


def test_carry_state_may_omit_values() -> None:
    def change(d: dict[str, Any]) -> None:
        d["scenes"][0]["acting"]["states"][1] = {
            "key": "st_2",
            "character_key": "char_alex",
            "source": "director",
            "span": d["scenes"][0]["acting"]["states"][1]["span"],
            "carry": True,
        }

    spec = VideoSpec.model_validate(mutate(change))
    assert spec.scenes[0].acting is not None and spec.scenes[0].acting.states[1].emotion is None
    with pytest.raises(ValidationError, match="required unless carry"):
        VideoSpec.model_validate(mutate(lambda d: d["scenes"][0]["acting"]["states"][1].pop("emotion")))


def test_json_schema_exports_with_discriminators() -> None:
    schema = VideoSpec.model_json_schema()
    text = json.dumps(schema)
    assert schema["title"] == "VideoSpec"
    assert '"discriminator"' in text
    assert "WordSpan" in schema["$defs"] and "ActingState" in schema["$defs"]
