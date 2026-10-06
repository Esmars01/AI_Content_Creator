"""I8 — Locks pin values (and routes for locked groups); patches to locked paths are rejected naming
the lock group (§12.7, §28 step 4).

Every lock group is exercised through the whole proposal path (operation → SpecPatch → lock check):
the proposal fails with a `locked` issue that names the group and the derived version is never
created. Locks are scoped (an out-of-scope edit passes), an instruction can never remove a lock,
regeneration of a blocked component is refused, and locked groups that pin routes keep the
parent's engine and delivered artifacts.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from ce_build import BuildOptions, build_graph
from ce_core.edit.locks import regenerate_refusals
from ce_core.spec.videospec import VideoSpec
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.edits import first_build, parent_build_of, propose_ops
from ce_testing.fixtures import ALEX, two_scene_spec_dict

pytestmark = [pytest.mark.invariant]

HOOK = {"scene_keys": ["scn_hook"]}


def base(*locks: dict[str, Any]) -> dict[str, Any]:
    data = two_scene_spec_dict()
    data["locks"] = [{"set_by": "user", "scope": {}, **lock} for lock in locks]
    return data


# (group, lock scope, an operation that changes a value the group covers)
CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    ("voice", {"character_keys": ["char_alex"]}, {"op": "set_cast", "voice_version_id": str(ALEX.VOICE_UK_VERSION_ID)}),
    (
        "voice",
        {"character_keys": ["char_alex"]},
        {"op": "set_cast", "voice_prosody": {"rate": 1.1, "pitch_semitones": 0.0, "energy": 0.6}},
    ),
    (
        "wardrobe",
        HOOK,
        {"op": "set_wardrobe", "scope": HOOK, "wardrobe_version_id": str(ALEX.WARDROBE_NAVY_VERSION_ID)},
    ),
    ("world", HOOK, {"op": "set_world_override", "scope": HOOK, "element_states": {"el_neon": "off"}}),
    ("world", HOOK, {"op": "set_world_binding", "scope": HOOK, "time_of_day": "evening"}),
    ("acting", HOOK, {"op": "set_acting", "scope": HOOK, "changes": {"strategies": {"gaze": "side_glance"}}}),
    ("intent", HOOK, {"op": "set_intent", "scope": HOOK, "fields": {"tension_level": 0.9}}),
    ("camera", {}, {"op": "set_camera", "add_moves": [{"type": "handheld_drift", "scale": 0.6}]}),
    ("music", {}, {"op": "set_music", "remove": ["mc_1"]}),
    ("sfx", {}, {"op": "set_sfx", "remove": ["sfx_1"]}),
    ("captions", {}, {"op": "set_captions", "changes": {"max_words_per_line": 2}}),
]


@pytest.mark.parametrize(("group", "scope", "op"), CASES, ids=[f"{c[0]}:{c[2]['op']}" for c in CASES])
def test_a_patch_to_a_locked_path_is_rejected_naming_the_group(group: str, scope: dict[str, Any], op: Any) -> None:
    unlocked = propose_ops(base(), [op])
    assert unlocked.status == "proposed", unlocked.issues  # the edit itself is valid …
    locked = propose_ops(base({"group": group, "scope": scope}), [op])
    assert locked.status == "failed" and locked.graph is None  # … but never reaches a derived version
    named = [i for i in locked.issues if i.code == "locked"]
    assert named and {i.detail["group"] for i in named} == {group}
    assert all(f"`{group}` lock" in i.message for i in named)


def test_the_script_lock_refuses_wording_changes() -> None:
    op = {"op": "edit_script", "segment_key": "seg_1", "text": "Everyone says AI agents are smarter chatbots."}
    assert propose_ops(base(), [op]).status == "proposed"
    locked = propose_ops(base({"group": "script"}), [op])
    assert locked.status == "failed" and locked.issues[0].code == "wording_locked"
    assert "`script` lock" in locked.issues[0].message


def test_locks_are_scoped() -> None:
    reveal = {"scene_keys": ["scn_reveal"]}
    op = {"op": "set_world_override", "scope": reveal, "element_states": {"el_neon": "off"}}
    assert propose_ops(base({"group": "world", "scope": HOOK}), [op]).status == "proposed"
    assert propose_ops(base({"group": "world", "scope": reveal}), [op]).status == "failed"


def test_an_instruction_never_removes_a_lock() -> None:
    data = base({"group": "voice", "scope": {"character_keys": ["char_alex"]}})
    removal = {"op": "set_lock", "remove": [{"group": "voice"}]}
    for actor in ("director", "system"):
        refused = propose_ops(data, [removal], actor=actor)
        assert refused.status == "failed" and refused.issues[0].code == "lock_removal_forbidden"
    by_user = propose_ops(data, [removal], allow_lock_removal=True)  # the lock endpoint, structured ops
    assert by_user.status == "proposed" and by_user.spec is not None and by_user.spec.locks == []


def test_regenerating_a_blocked_component_is_refused_naming_the_lock() -> None:
    vocab = config_bundle().vocab
    world = [{"group": "world", "scope": HOOK, "set_by": "user"}]
    (refusal,) = regenerate_refusals(["background"], world, vocab, scenes=["scn_hook"])
    assert refusal.groups == ("world",) and "world" in refusal.message
    assert regenerate_refusals(["background"], world, vocab, scenes=["scn_reveal"]) == []
    proposal = propose_ops(base(*world), [{"op": "regenerate", "scope": HOOK, "components": ["background"]}])
    assert proposal.status == "failed" and proposal.issues[0].code == "regenerate_refused"


def test_a_voice_lock_pins_the_delivered_audio_and_its_route() -> None:
    """Behavior edits under a voice lock re-render pictures, never the voice: the compiled voice
    output is reused by content and no TTS, ASR or alignment node runs."""
    data = base({"group": "voice", "scope": {"character_keys": ["char_alex"]}})
    op = {
        "op": "set_acting",
        "scope": {"scene_keys": ["scn_reveal"]},
        "changes": {"strategies": {"prosody": "assertive_light"}},
    }
    proposal = propose_ops(data, [op])
    assert proposal.status == "proposed", proposal.issues
    runs = {*proposal.impact["regenerate"], *proposal.impact["cascade"]}
    assert not {k for k in runs if k.startswith(("tts.", "asr.", "align.", "voice."))}
    assert proposal.graph is not None
    reused = [n for n in proposal.graph.nodes if n.kind == "behavior.compile_voice" and n.params.get("reuse")]
    assert {n.segment_key for n in reused} == {"seg_1", "seg_2"}
    assert any(b.get("group") == "voice" for b in proposal.impact["locks_blocking"])


def test_route_pinning_follows_the_lock_groups_that_pin_routes() -> None:
    """A locked group with `pins_routes` keeps its parent's engine even when the catalog would now
    choose another; without the lock the dirty node re-routes."""
    bundle = config_bundle()
    catalog = mock_catalog()
    second = catalog.manifests["mock_voice"].model_copy(update={"id": "mock_voice_b"})
    favoured = replace(
        catalog,
        manifests={**catalog.manifests, "mock_voice_b": second},
        quality={**catalog.quality, ("mock_voice_b", "voice.tts"): 1.0, ("mock_voice", "voice.tts"): 0.0},
    )
    data = base({"group": "voice", "scope": {"character_keys": ["char_alex"]}})
    parent = first_build(data)
    pb = parent_build_of(parent)
    data["script"]["segments"][0]["text"] = "Everyone thinks AI agents are only smarter chatbots."
    data["script"]["segments"][1]["text"] = "But here's the catch... they're not."  # every segment dirty
    locked = build_graph(VideoSpec.model_validate(data), example_build_refs(), bundle, favoured, parent=pb).by_key()
    route = locked["tts.segment:seg_1"].route
    assert route is not None and route.adapter_id == "mock_voice" and "voice lock" in route.reason
    data["locks"] = []
    free = build_graph(
        VideoSpec.model_validate(data), example_build_refs(), bundle, favoured, parent=pb, options=BuildOptions()
    ).by_key()
    assert free["tts.segment:seg_1"].route is not None and free["tts.segment:seg_1"].route.adapter_id == "mock_voice_b"
