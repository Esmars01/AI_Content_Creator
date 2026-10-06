"""Adapter behavior translation suite [3, 8] (§37): golden translations of the §11 example per mock
translator; every compiled item is either encoded or reported unsupported, never dropped.

Regenerate the golden files after an intended translator change with `CE_UPDATE_GOLDEN=1`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from ce_behavior.directives import to_directives
from ce_behavior.scene import SceneWords
from ce_contracts import models as m
from ce_contracts.common import ArtifactRef
from ce_contracts.plugins import discover
from ce_core.behavior.compiled import CompiledBehavior
from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, catalog_with, version_behavior

pytestmark = pytest.mark.behavior

GOLDEN = Path(__file__).parent / "golden"
REGISTRY = discover(app_env="test", include_mocks=True)
REF = ArtifactRef(sha256="0" * 64, kind="image", mime="image/png", role="keyframe")


def _times(words: SceneWords) -> dict[tuple[str, int], tuple[float, float]]:
    return {w: (round(0.1 + 0.33 * i, 3), round(0.38 + 0.33 * i, 3)) for i, w in enumerate(words.order)}


def _compiled(disabled: frozenset[str], target_key: str) -> tuple[CompiledBehavior, Any, SceneWords]:
    vb = version_behavior(catalog_with(disabled))
    spec = vb.spec
    words = SceneWords.of(spec, spec.scenes[0])
    compiled = next(c for c in vb.compiled if c.target_key == target_key)
    return compiled, vb.cbs["scn_hook"], words


def _engine(adapter: str, request: Any) -> dict[str, Any]:
    translator = REGISTRY.get(adapter).translator()
    assert translator is not None
    out: Any = translator.translate(request.behavior, request)
    assert type(out) is type(request) and out.behavior == request.behavior
    return {"translator": translator.version, "engine": out.engine}


def _avatar(disabled: frozenset[str], adapter: str) -> dict[str, Any]:
    compiled, cbs, words = _compiled(disabled, "sht_1:c1")
    directives = to_directives(compiled, cbs, words=words, word_times=_times(words))
    request = m.AvatarRequest(keyframe=REF, audio=REF, behavior=directives, fps=25, width=720, height=1280)
    return _engine(adapter, request)


def _voice(segment: str) -> dict[str, Any]:
    compiled, cbs, words = _compiled(GLOBAL_ONLY, segment)
    directives = to_directives(compiled, cbs, words=words, word_times={})
    request = m.TTSRequest(text="placeholder", language="en", behavior=directives)
    return _engine("mock_voice", request)


CASES = {
    "mock_global_v1": lambda: _avatar(GLOBAL_ONLY, "mock_avatar_global"),
    "mock_segment_v1": lambda: _avatar(SEGMENT_ONLY, "mock_avatar_segment"),
    "mock_voice_v2.seg_1": lambda: _voice("seg_1"),
    "mock_voice_v2.seg_2": lambda: _voice("seg_2"),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_translation(name: str) -> None:
    produced = json.loads(json.dumps(CASES[name](), sort_keys=True))
    path = GOLDEN / f"{name}.json"
    if os.environ.get("CE_UPDATE_GOLDEN") == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert path.exists(), f"golden file missing: {path} (run with CE_UPDATE_GOLDEN=1)"
    assert produced == json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("disabled", "adapter", "target"),
    [
        (GLOBAL_ONLY, "mock_avatar_global", "sht_1:c1"),
        (SEGMENT_ONLY, "mock_avatar_segment", "sht_1:c1"),
        (GLOBAL_ONLY, "mock_voice", "seg_1"),
        (GLOBAL_ONLY, "mock_voice", "seg_2"),
    ],
)
def test_unsupported_items_are_reported_never_dropped(disabled: frozenset[str], adapter: str, target: str) -> None:
    compiled, cbs, words = _compiled(disabled, target)
    if target.startswith("sht_"):
        directives = to_directives(compiled, cbs, words=words, word_times=_times(words))
        request: Any = m.AvatarRequest(keyframe=REF, audio=REF, behavior=directives, fps=25, width=720, height=1280)
    else:
        directives = to_directives(compiled, cbs, words=words, word_times={})
        request = m.TTSRequest(text="placeholder", language="en", behavior=directives)
    engine = _engine(adapter, request)["engine"]
    requested = {(r.item_ref, r.dimension) for r in compiled.realizations}
    encoded = {(e["item_ref"], e["dimension"]) for e in engine["encoded"]}
    unsupported = {(e["item_ref"], e["dimension"]) for e in engine["unsupported"]}
    assert encoded | unsupported == requested and not encoded & unsupported
    for entry in engine["unsupported"]:
        assert entry["reason"], entry  # every unsupported item says why
    omitted = {(r.item_ref, r.dimension) for r in compiled.realizations if r.method == "omit"}
    assert omitted <= unsupported
