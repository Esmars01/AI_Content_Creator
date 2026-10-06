"""Spec templates (Phase 12, ADR 0059): portable slots per kind, capture from a version, strict
bodies, composition (later wins, `merge: append` lists, conflicts reported) and apply as ordinary
edit operations that translate cleanly and are idempotent."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any

import pytest
from ce_core.edit.ops import parse_operations
from ce_core.edit.translate import TranslateContext, translate
from ce_core.spec.templates import (
    KIND_SLOTS,
    TemplateError,
    body_digest,
    capture,
    compose,
    operations,
    select_slots,
    validate_body,
)
from ce_core.spec.videospec import VideoSpec
from ce_core.vocab import load_vocabulary
from ce_testing.fixtures import ALEX, example_spec_dict, home_office_world

VOCAB = load_vocabulary(Path(__file__).resolve().parents[4] / "config" / "vocab")
WORLDS = {str(ALEX.WORLD_VERSION_ID): home_office_world().model_dump(mode="json")}
KIT = str(uuid.UUID(int=7))


def apply(ops: list[dict[str, Any]], doc: dict[str, Any]) -> dict[str, Any]:
    return translate(parse_operations(ops), doc, TranslateContext(vocab=VOCAB, worlds=WORLDS)).document


def test_kinds_hold_their_slots_and_paths_narrow_them() -> None:
    assert select_slots("camera") == list(KIND_SLOTS["camera"])
    assert select_slots("video", ["/captions"]) == [p for p in KIND_SLOTS["video"] if p.startswith("/captions/")]
    assert select_slots("brand", ["brand/logo_overlay"]) == ["/brand/logo_overlay"]
    with pytest.raises(TemplateError, match="unknown template kind"):
        select_slots("music")
    with pytest.raises(TemplateError, match="names no camera template slot"):
        select_slots("camera", ["/captions/style_id"])
    with pytest.raises(TemplateError):
        select_slots("video", ["/scenes/0/shots"])  # nothing keyed to one video is a slot


def test_capture_takes_only_portable_values() -> None:
    spec = example_spec_dict()
    spec["brand"] = {"brand_kit_id": KIT, "logo_overlay": True}
    body = capture(spec, "video")
    assert body["captions"]["style_id"] == spec["captions"]["style_id"]
    assert body["brand"] == {"brand_kit_id": KIT, "logo_overlay": True}
    talking = [sh["camera"] for sc in spec["scenes"] for sh in sc["shots"] if sh["type"] == "talking_head"]
    assert body["shot_defaults"]["camera"]["profile_id"] == talking[0]["profile_id"]
    assert "scenes" not in body and "script" not in body and "cast" not in body
    assert validate_body(body, "video") == body  # what is captured is a valid body
    caption = capture(spec, "caption")
    assert set(caption) == {"captions"}
    assert set(capture(spec, "brand")) == {"brand", "captions"}


def test_bodies_are_strict() -> None:
    with pytest.raises(TemplateError) as unknown:
        validate_body({"captions": {"style_id": "bold_pop_highlight", "font": "x"}})
    assert unknown.value.problems[0][0] == "/captions/font"
    with pytest.raises(TemplateError):
        validate_body({"scenes": []})
    with pytest.raises(TemplateError):
        validate_body({"meta": {"target_duration_s": -3}})
    with pytest.raises(TemplateError, match="a camera template cannot hold /captions/style_id"):
        validate_body({"captions": {"style_id": "bold_pop_highlight"}}, "camera")
    a, b = {"captions": {"enabled": True, "style_id": "x"}}, {"captions": {"style_id": "x", "enabled": True}}
    assert body_digest(a) == body_digest(b)


def test_composition_later_wins_appends_lists_and_reports_conflicts() -> None:
    base = {
        "captions": {"style_id": "bold_pop_highlight", "max_words_per_line": 3},
        "meta": {"platform_targets": ["tiktok"]},
        "render": {"outputs": [{"preset_id": "tiktok_1080x1920", "aspect": "9:16"}]},
    }
    brand = {"captions": {"style_id": "minimal_lower"}, "brand": {"brand_kit_id": KIT}}
    own = {
        "meta": {"platform_targets": ["youtube_shorts", "tiktok"]},
        "render": {
            "outputs": [
                {"preset_id": "tiktok_1080x1920", "aspect": "9:16"},
                {"preset_id": "youtube_shorts_1080x1920", "aspect": "9:16"},
            ]
        },
    }
    out = compose([("base", base), ("brand", brand), ("own", own)])
    assert out.body["captions"] == {"style_id": "minimal_lower", "max_words_per_line": 3}
    assert out.body["meta"]["platform_targets"] == ["tiktok", "youtube_shorts"]
    assert [o["preset_id"] for o in out.body["render"]["outputs"]] == ["tiktok_1080x1920", "youtube_shorts_1080x1920"]
    assert [c.as_dict() for c in out.conflicts] == [
        {
            "path": "/captions/style_id",
            "values": [
                {"template_id": "base", "value": "bold_pop_highlight"},
                {"template_id": "brand", "value": "minimal_lower"},
            ],
            "winner": "minimal_lower",
        }
    ]
    assert out.sources["/brand/brand_kit_id"] == "brand" and out.sources["/meta/platform_targets"] == "own"
    reversed_order = compose([("brand", brand), ("base", base)])
    assert reversed_order.body["captions"]["style_id"] == "bold_pop_highlight"  # order decides


def test_apply_is_ordinary_edits_and_idempotent() -> None:
    spec = example_spec_dict()
    template_id = str(uuid.UUID(int=99))
    body = {
        "captions": {"style_id": "minimal_lower", "highlight": "phrase"},
        "brand": {"brand_kit_id": KIT, "logo_overlay": True},
        "provenance": {"visible_label": "on"},
        "shot_defaults": {"camera": {"framing": "close_up"}},
        "scene_defaults": {"pacing": {"cut_cadence": "fast"}},
        "meta": {"platform_targets": ["youtube_shorts"]},
    }
    ops = operations(body, spec, reason="template: punchy", template_ids=[template_id])
    assert {o["op"] for o in ops} == {
        "set_meta",
        "set_captions",
        "set_brand",
        "set_provenance_label",
        "set_camera",
        "set_pacing",
    }
    before = copy.deepcopy(spec)
    doc = apply(ops, spec)
    assert spec == before  # translation never mutates its input
    VideoSpec.model_validate(doc)
    assert doc["captions"]["style_id"] == "minimal_lower" and doc["captions"]["highlight"] == "phrase"
    assert doc["brand"] == {"brand_kit_id": KIT, "logo_overlay": True}
    assert doc["provenance"]["visible_label"] == "on"
    assert template_id in doc["meta"]["template_ids"]
    assert set(doc["meta"]["platform_targets"]) >= {"youtube_shorts", *spec["meta"]["platform_targets"]}
    talking = [sh for sc in doc["scenes"] for sh in sc["shots"] if sh["type"] == "talking_head"]
    assert talking and all(sh["camera"]["framing"] == "close_up" for sh in talking)
    assert all(sc["pacing"]["cut_cadence"] == "fast" for sc in doc["scenes"])
    assert operations(body, doc, reason="again", template_ids=[template_id]) == []  # already applied


def test_a_template_that_clears_the_brand_kit() -> None:
    spec = example_spec_dict()
    spec["brand"] = {"brand_kit_id": KIT, "logo_overlay": False}
    ops = operations({"brand": {"logo_overlay": True}}, spec, reason="r")
    assert ops == [{"op": "set_brand", "logo_overlay": True, "reason": "r"}]  # the kit stays
