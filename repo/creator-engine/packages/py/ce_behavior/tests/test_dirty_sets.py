"""Exact dirty sets (§12.9, §19.5; Phase 6 DoD; §37 "Dirty analysis: behavior-only changes" and
"Dirty analysis: World DNA changes"): for each kind of edit, the exact set of nodes a derived
version runs again, with the behavior evaluator. Everything else keeps the parent's artifacts.

The base is the example split into two scenes (`two_scene_spec_dict`): `scn_hook` (seg_1, shot
sht_1 with two takes) films from `cam_desk_front`, whose layout shows the shelf, the neon sign
and the window; `scn_reveal` (seg_2, talking shot sht_4 and the world-bound B-roll overlay sht_2)
films from `cam_side_wide`, which has no layout (every element counts as visible).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from ce_build.refs import AssetInfo, WorldRef
from ce_testing.build import example_build_refs
from ce_testing.edits import Built, derive, first_build
from ce_testing.fixtures import ALEX, two_scene_spec_dict

pytestmark = pytest.mark.behavior

PRESET = "tiktok_1080x1920_30"
RENDER = {
    f"render.final:{PRESET}",
    f"provenance.watermark_video:{PRESET}",
    f"provenance.watermark_audio:{PRESET}",
    f"provenance.sign:{PRESET}",
    f"qc.render:{PRESET}",
    "render.proxy:proxy",
    "behavior.coverage:video",
    "qc.continuity:video",
}
HOOK_SHOT = {
    "image.keyframe:sht_1",
    "avatar.render:sht_1:c1:t1",
    "avatar.render:sht_1:c1:t2",
    "behavior.observe:sht_1:t1",
    "behavior.observe:sht_1:t2",
    "qc.shot:sht_1:t1",
    "qc.shot:sht_1:t2",
    "post.expression:sht_1",
    "post.camera:sht_1",
    "post.realism:sht_1",
    "qc.world:sht_1",
}
REVEAL_SHOT = {
    "image.keyframe:sht_4",
    "avatar.render:sht_4:c1:t1",
    "behavior.observe:sht_4:t1",
    "qc.shot:sht_4:t1",
    "post.expression:sht_4",
    "post.camera:sht_4",
    "post.realism:sht_4",
    "qc.world:sht_4",
}
REVEAL_BROLL = {"video.broll:sht_2:t1", "qc.shot:sht_2:t1", "post.camera:sht_2", "post.realism:sht_2", "qc.world:sht_2"}
VOICE_2 = {"tts.segment:seg_2", "asr.verify:seg_2", "align.segment:seg_2"}
GENERATION = ("tts.", "voice.", "image.", "avatar.", "video.", "world.plate", "audio.music", "audio.sfx")


def _base(*, voice_locked: bool = False) -> dict[str, Any]:
    data = two_scene_spec_dict()
    if not voice_locked:
        data["locks"] = []
    return data


def _derive(mutate: Callable[[dict[str, Any]], None], *, voice_locked: bool = False, **kwargs: Any) -> Built:
    base = _base(voice_locked=voice_locked)
    parent = first_build(base, **{k: v for k, v in kwargs.items() if k != "refs"})
    child = copy.deepcopy(base)
    mutate(child)
    return derive(parent, child, **kwargs)


def _runs(built: Built) -> set[str]:
    assert built.impact is not None
    return set(built.impact.executes)


def _generated(built: Built) -> set[str]:
    return {k for k in _runs(built) if k.startswith(GENERATION)}


def _reveal_state(d: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = d["scenes"][1]["acting"]["states"][0]
    return state


def _more_skeptical(d: dict[str, Any]) -> None:
    state = _reveal_state(d)
    state["emotion"]["displayed"] = {"label": "skeptical", "intensity": 0.8}
    state["emotion"]["felt"] = {"label": "skeptical", "intensity": 0.8}
    state["strategies"]["gaze"] = "side_glance"


# ---------------------------------------------------------------------- behavior-only edits


def test_behavior_edit_with_voice_unlocked_re_performs_that_scene_only() -> None:
    built = _derive(_more_skeptical)
    assert built.impact is not None
    assert built.impact.regenerate == ["behavior.resolve:scn_reveal"]
    assert _runs(built) == {
        "behavior.resolve:scn_reveal",
        "behavior.compile_voice:seg_2",
        *VOICE_2,
        "audio.room:scn_reveal",
        "behavior.keyframe_state:sht_4",
        "behavior.compile_visual:sht_4:c1",
        *REVEAL_SHOT,
        *REVEAL_BROLL,  # its length follows the re-timed dialogue
        "captions.build:en",
        "mix.audio:main",
        *RENDER,
    }
    assert built.impact.estimate["model_nodes"] > 0 and built.impact.no_visible_effect == []


def test_behavior_edit_with_voice_locked_keeps_the_delivered_audio() -> None:
    built = _derive(_more_skeptical, voice_locked=True)
    assert built.impact is not None
    compile_voice = built.graph.by_key()["behavior.compile_voice:seg_2"]
    assert compile_voice.params["reuse"] and compile_voice.deps == []  # the parent's prosody, by content
    assert "behavior.compile_voice:seg_2" in built.impact.keep
    assert _runs(built) == {
        "behavior.resolve:scn_reveal",
        "behavior.keyframe_state:sht_4",
        "behavior.compile_visual:sht_4:c1",
        *REVEAL_SHOT,
        "qc.shot:sht_2:t1",  # the B-roll take is re-judged against the new requests, not regenerated
        "post.camera:sht_2",
        "post.realism:sht_2",
        "qc.world:sht_2",
        *RENDER,
    }
    assert not _runs(built) & {*VOICE_2, "video.broll:sht_2:t1", "captions.build:en", "mix.audio:main"}


def test_a_change_the_engine_cannot_show_has_no_visible_effect() -> None:
    def smile_intensity(d: dict[str, Any]) -> None:  # small_smile is UNSUPPORTED on the routed engine
        d["scenes"][1]["acting"]["events"][1]["intensity"] = 0.35

    built = _derive(smile_intensity)
    assert built.impact is not None
    assert _generated(built) == set()
    assert {e["node_key"] for e in built.impact.no_visible_effect} == {
        "behavior.compile_voice:seg_2",
        "behavior.compile_visual:sht_4:c1",
        "behavior.keyframe_state:sht_4",
    }
    # only the cheap judgement and what follows from it run again (post.expression is the MVP's
    # pass-through expression editor, §12.1)
    assert built.impact.estimate["model_nodes"] == 1
    assert _runs(built) - {
        "behavior.resolve:scn_reveal",
        "behavior.compile_voice:seg_2",
        "behavior.compile_visual:sht_4:c1",
        "behavior.keyframe_state:sht_4",
    } == {
        "qc.shot:sht_4:t1",
        "post.expression:sht_4",
        "post.camera:sht_4",
        "post.realism:sht_4",
        "qc.world:sht_4",
        "qc.shot:sht_2:t1",
        "post.camera:sht_2",
        "post.realism:sht_2",
        "qc.world:sht_2",
        *RENDER,
    }


def test_a_voice_locked_text_edit_resynthesizes_only_the_changed_segment_on_the_pinned_route() -> None:
    def new_text(d: dict[str, Any]) -> None:
        d["script"]["segments"][1]["text"] = "But here's the catch... they're not."

    built = _derive(new_text, voice_locked=True)
    by_key = built.graph.by_key()
    assert "reuse" in by_key["behavior.compile_voice:seg_1"].params
    assert "reuse" not in by_key["behavior.compile_voice:seg_2"].params  # changed text compiles anew
    assert {"tts.segment:seg_2", "asr.verify:seg_2", "align.segment:seg_2"} <= _runs(built)
    assert not _runs(built) & {"tts.segment:seg_1", "asr.verify:seg_1", "align.segment:seg_1"}
    route = by_key["tts.segment:seg_2"].route
    assert route is not None and route.pinned


# ---------------------------------------------------------------------- World DNA changes (§19.5)


def _world_version(changes: Callable[[dict[str, Any]], None], *, new_side_plate: bool = True) -> tuple[Any, Any]:
    """Refs with a home-office v2 (DNA changed by `changes`, optionally a new cam_side_wide plate)."""
    refs = example_build_refs()
    v1 = refs.worlds[ALEX.WORLD_VERSION_ID]
    dna = copy.deepcopy(dict(v1.dna))
    changes(dna)
    plates = {cam: {tod: dict(ws) for tod, ws in tods.items()} for cam, tods in v1.plates.items()}
    assets = dict(refs.assets)
    if new_side_plate:
        new_plate = AssetInfo(ALEX.PLATE_OFFICE_WIDE_ASSET_ID, "f" * 64, "seed/side_v2.png", "image/png", 10)
        plates["cam_side_wide"]["late_afternoon"]["clear"] = new_plate
        assets[new_plate.asset_id] = new_plate
    v2_id = ALEX.OFFICE_WORLD_VERSION_ID  # any approved id; only content enters keys
    worlds = {**refs.worlds, v2_id: WorldRef(v2_id, dna, plates)}
    return replace(refs, worlds=worlds, assets=assets), v2_id


def _bind_reveal_to(version_id: Any) -> Callable[[dict[str, Any]], None]:
    return lambda d: d["scenes"][1]["world"].update(world_version_id=str(version_id))


def test_a_new_world_version_with_another_plate() -> None:
    refs, v2 = _world_version(lambda dna: dna.update(palette=["#202020", "#e0d0b0", "#ff8a3d"]))
    built = _derive(_bind_reveal_to(v2), refs=refs)
    assert _runs(built) == {"world.plate:scn_reveal", *REVEAL_SHOT, *REVEAL_BROLL, *RENDER}


def test_a_new_world_version_with_other_acoustics_reprocesses_the_room() -> None:
    refs, v2 = _world_version(lambda dna: dna["acoustics"].update(rt60_s=0.6))
    built = _derive(_bind_reveal_to(v2), refs=refs)
    assert _runs(built) == {
        "world.plate:scn_reveal",
        *REVEAL_SHOT,
        *REVEAL_BROLL,
        "audio.room:scn_reveal",
        "mix.audio:main",
        *RENDER,
    }


def test_a_new_world_version_changing_the_behavior_digest_re_resolves_the_scene() -> None:
    def new_zone_posture(dna: dict[str, Any]) -> None:
        dna["zones"][0]["allowed_postures"] = [*dna["zones"][0]["allowed_postures"], "standing_upright"]

    refs, v2 = _world_version(new_zone_posture, new_side_plate=False)
    built = _derive(_bind_reveal_to(v2), refs=refs)
    assert "behavior.resolve:scn_reveal" in _runs(built)
    assert _generated(built) == set()  # zones change behavior, never a plate


def test_a_world_version_change_outside_the_bound_position_keeps_the_plate() -> None:
    def other_position(dna: dict[str, Any]) -> None:  # only the cam_desk_front layout changes
        dna["background_layouts"]["cam_desk_front"]["composition"] = "shelf slightly left of the head"

    refs, v2 = _world_version(other_position, new_side_plate=False)
    built = _derive(lambda d: d["scenes"][0]["world"].update(world_version_id=str(v2)), refs=refs)
    assert "world.plate:scn_hook" in _runs(built)  # the front layout is the hook's position
    built = _derive(_bind_reveal_to(v2), refs=refs)
    assert _runs(built) == set()  # nothing the side position shows changed


def _overrides(scene: int) -> Callable[[dict[str, Any]], dict[str, Any]]:
    return lambda d: d["scenes"][scene]["world"]["overrides"]


def test_a_lighting_override_only() -> None:
    def lighting(d: dict[str, Any]) -> None:
        _overrides(1)(d)["lighting"] = {
            "color_temp_k_delta": -300,
            "luminance_delta": 0.05,
            "key_azimuth_deg_delta": 0,
            "deviation_declared": False,
        }

    built = _derive(lighting)
    assert _runs(built) == {"world.plate:scn_reveal", *REVEAL_SHOT, *REVEAL_BROLL, *RENDER}


def test_an_acoustics_override_only() -> None:
    built = _derive(lambda d: _overrides(1)(d).update(acoustics={"ambient_additions": ["street"]}))
    assert _runs(built) == {"audio.room:scn_reveal", "mix.audio:main", *RENDER}


@pytest.mark.parametrize(
    ("scene", "element", "expected"),
    [
        (0, "el_monitor", set()),  # not visible from cam_desk_front, not a light
        (0, "el_neon", {"world.plate:scn_hook", *HOOK_SHOT, *RENDER}),  # visible (and a practical light)
        (1, "el_monitor", {"world.plate:scn_reveal", *REVEAL_SHOT, *REVEAL_BROLL, *RENDER}),  # no layout: visible
    ],
)
def test_an_element_state_change(scene: int, element: str, expected: set[str]) -> None:
    built = _derive(lambda d: _overrides(scene)(d)["element_states"].update({element: "off"}))
    assert _runs(built) == expected


def test_an_element_state_change_of_an_attention_target_re_resolves_the_scene() -> None:
    def look_at_monitor(d: dict[str, Any]) -> None:
        _reveal_state(d)["attention_target"] = "el_monitor"

    base = _base()
    look_at_monitor(base)
    parent = first_build(base)
    child = copy.deepcopy(base)
    _overrides(1)(child)["element_states"]["el_monitor"] = "off"
    built = derive(parent, child)
    assert built.impact is not None and "behavior.resolve:scn_reveal" in built.impact.regenerate
    assert {"world.plate:scn_reveal", *REVEAL_SHOT, *REVEAL_BROLL} <= _runs(built)


def test_a_camera_position_change() -> None:
    built = _derive(lambda d: d["scenes"][1]["world"].update(camera_position_key="cam_desk_front"))
    assert _runs(built) == {"world.plate:scn_reveal", *REVEAL_SHOT, *REVEAL_BROLL, *RENDER}
    assert not _runs(built) & {*VOICE_2, "audio.room:scn_reveal", "mix.audio:main"}


@pytest.mark.parametrize("change", [{"time_of_day": "evening"}, {"weather": "overcast"}])
def test_a_time_of_day_or_weather_change(change: dict[str, str]) -> None:
    built = _derive(lambda d: d["scenes"][0]["world"].update(change))
    assert _runs(built) == {"world.plate:scn_hook", *HOOK_SHOT, *RENDER}
    assert "audio.room:scn_hook" not in _runs(built)  # the ambient profile does not depend on them


@pytest.mark.parametrize(
    ("mutate", "expected_resolve"),
    [
        (lambda o: o["hide_elements"].append("el_window"), True),  # visible; a window is an attention target
        (lambda o: o["add_elements"].append(
            {"key": "el_x_lamp", "kind": "light", "label": "desk lamp", "description": "", "position": [0.8, 0.6, 0.8]}
        ), False),
    ],
)  # fmt: skip
def test_add_or_hide_an_element_visible_from_the_position(mutate: Any, expected_resolve: bool) -> None:
    built = _derive(lambda d: mutate(_overrides(0)(d)))
    assert {"world.plate:scn_hook", *HOOK_SHOT, *RENDER} <= _runs(built)
    assert ("behavior.resolve:scn_hook" in _runs(built)) is expected_resolve
    assert _generated(built) == {"world.plate:scn_hook", "image.keyframe:sht_1"} | {
        k for k in HOOK_SHOT if k.startswith("avatar.")
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o["hide_elements"].append("el_mug"),
        lambda o: o["move_elements"].append({"key": "el_mug", "position": [0.6, 0.5, 0.76]}),
    ],
)
def test_hide_or_move_an_element_out_of_view_changes_nothing(mutate: Any) -> None:
    assert _runs(_derive(lambda d: mutate(_overrides(0)(d)))) == set()


def test_a_continuity_ref_change() -> None:
    def to_asset(d: dict[str, Any]) -> None:
        d["scenes"][0]["world"]["continuity_ref"] = {"kind": "asset", "asset_id": str(ALEX.PLATE_SIDE_ASSET_ID)}

    built = _derive(to_asset)
    assert _runs(built) == {"world.plate:scn_hook", *HOOK_SHOT, *RENDER}


# ---------------------------------------------------------------------- accent, camera, wardrobe, pacing


def test_an_accent_change_resynthesizes_the_voice_and_re_renders_lip_sync() -> None:
    built = _derive(lambda d: d["cast"][0]["overrides"].update(voice_version_id=str(ALEX.VOICE_UK_VERSION_ID)))
    runs = _runs(built)
    assert {"voice.prepare:char_alex", "tts.segment:seg_1", "tts.segment:seg_2", *VOICE_2} <= runs
    assert {"avatar.render:sht_1:c1:t1", "avatar.render:sht_1:c1:t2", "avatar.render:sht_4:c1:t1"} <= runs
    assert not runs & {
        "behavior.resolve:scn_hook",
        "behavior.resolve:scn_reveal",
        "behavior.compile_voice:seg_1",
        "behavior.compile_voice:seg_2",
        "world.plate:scn_hook",
        "world.plate:scn_reveal",
        "image.keyframe:sht_1",
        "image.keyframe:sht_4",
        "audio.sfx:sfx_1",
    }


def test_a_slightly_handheld_camera_is_post_only() -> None:
    def handheld(d: dict[str, Any]) -> None:
        for scene in d["scenes"]:
            for shot in scene["shots"]:
                if shot["type"] == "talking_head":
                    shot["camera"]["moves"].append(
                        {
                            "key": f"mv_h{shot['key'][-1]}",
                            "type": "handheld_drift",
                            "at": shot["span"]["start"],
                            "scale": 0.6,
                            "transition": "smooth",
                            "derived_from": [],
                        }
                    )

    built = _derive(handheld)
    assert built.impact is not None
    assert set(built.impact.regenerate) == {"post.camera:sht_1", "post.camera:sht_4"}
    assert _runs(built) == {
        "post.camera:sht_1",
        "post.realism:sht_1",
        "qc.world:sht_1",
        "post.camera:sht_4",
        "post.realism:sht_4",
        "qc.world:sht_4",
        *RENDER,
    }


def test_a_camera_profile_change_regenerates_that_shot() -> None:
    built = _derive(lambda d: d["scenes"][0]["shots"][0]["camera"].update(profile_id="webcam"))
    # the webcam also records through another mic (phone_front_mic → webcam_mic): the scene's room
    # sound and the mix follow (audit OUT-ROOM — before, they stayed cached with the old mic)
    assert _runs(built) == {*HOOK_SHOT, *RENDER, "audio.room:scn_hook", "mix.audio:main"}


def test_a_wardrobe_change_keeps_the_face_voice_and_room() -> None:
    def navy(d: dict[str, Any]) -> None:
        for scene in d["scenes"]:
            scene["cast"][0]["wardrobe_version_id"] = str(ALEX.WARDROBE_NAVY_VERSION_ID)

    built = _derive(navy)
    assert _runs(built) == {*HOOK_SHOT, *REVEAL_SHOT, *RENDER}


def test_pacing_cut_cadence_is_editorial() -> None:
    built = _derive(lambda d: d["scenes"][1].update(pacing={"target_wpm_delta": 0.0, "cut_cadence": "fast"}))
    assert _runs(built) == {
        "post.camera:sht_4",
        "post.realism:sht_4",
        "qc.world:sht_4",
        "post.camera:sht_2",
        "post.realism:sht_2",
        "qc.world:sht_2",
        *RENDER,
    }
    assert _generated(built) == set()


def test_pacing_wpm_is_a_re_performance() -> None:
    built = _derive(lambda d: d["scenes"][1].update(pacing={"target_wpm_delta": 0.15, "cut_cadence": None}))
    assert built.impact is not None
    assert set(built.impact.regenerate) == {"tts.segment:seg_2"}
    assert _runs(built) == {
        *VOICE_2,
        "audio.room:scn_reveal",
        "behavior.compile_visual:sht_4:c1",
        *(REVEAL_SHOT - {"image.keyframe:sht_4"}),
        *REVEAL_BROLL,
        "captions.build:en",
        "mix.audio:main",
        *RENDER,
    }
    assert built.graph.by_key()["tts.segment:seg_2"].params["wpm"] == pytest.approx(150 * 1.15)


def test_pacing_wpm_under_a_voice_lock_keeps_the_delivered_audio() -> None:
    built = _derive(
        lambda d: d["scenes"][1].update(pacing={"target_wpm_delta": 0.15, "cut_cadence": None}), voice_locked=True
    )
    assert _runs(built) == set()  # the voice lock pins the delivered audio, pace included (§12.7)
