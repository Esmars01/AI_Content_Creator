"""Build graph (§12.1): node kinds, keys, routes, chunking, determinism and content-only digests."""

from __future__ import annotations

import copy
import itertools
from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from ce_build import KINDS, BuildOptions, GraphError, build_graph, cache_key, generation_size
from ce_build.refs import AssetInfo, BuildRefs
from ce_core.spec.videospec import VideoSpec
from ce_testing.build import config_bundle, example_build_refs, mock_catalog
from ce_testing.fixtures import ALEX, example_spec, example_spec_dict, words

BUNDLE = config_bundle()
SCREEN_ASSET = UUID("0192f0a0-0000-7000-8000-0000000000d1")
MUSIC_ASSET = UUID("0192f0a0-0000-7000-8000-0000000000d2")
SFX_ASSET = UUID("0192f0a0-0000-7000-8000-0000000000d3")
BROLL_ASSET = UUID("0192f0a0-0000-7000-8000-0000000000d4")


def build(spec: VideoSpec | None = None, refs: BuildRefs | None = None, **options: Any) -> Any:
    return build_graph(
        spec or example_spec(), refs or example_build_refs(), BUNDLE, mock_catalog(), options=BuildOptions(**options)
    )


def kitchen_sink_dict() -> dict[str, Any]:
    """The example plus a second scene that exercises every remaining node kind."""
    data = example_spec_dict()
    data["meta"]["quality_tier"] = "final"
    data["meta"]["target_duration_s"] = 10
    data["script"]["segments"].append(
        {
            "key": "seg_3",
            "speaker_key": "char_alex",
            "text": "Here is what that looks like on my screen.",
            "annotations": [],
        }
    )
    data["scenes"].append(
        {
            "key": "scn_demo",
            "purpose": "demonstration",
            "order": 2,
            "segment_keys": ["seg_3"],
            "world": {
                "world_version_id": str(ALEX.WORLD_VERSION_ID),
                "camera_position_key": "cam_desk_front",
                "time_of_day": "evening",
                "weather": "clear",
            },
            "cast": [{"character_key": "char_alex", "wardrobe_version_id": str(ALEX.WARDROBE_VERSION_ID)}],
            "shots": [
                {
                    "key": "sht_3",
                    "type": "talking_head",
                    "layer": "base",
                    "span": words("seg_3", 0, 8),
                    "character_key": "char_alex",
                    "camera": {"profile_id": "webcam", "framing": "medium_close_up", "angle": "eye_level"},
                },
                {
                    "key": "sht_4",
                    "type": "screen",
                    "layer": "overlay",
                    "span": {
                        "kind": "duration",
                        "after": {"segment_key": "seg_3", "edge": "start"},
                        "duration_ms": 1500,
                    },
                    "camera": {"profile_id": "webcam", "framing": "insert", "angle": "eye_level"},
                    "screen": {"asset_id": str(SCREEN_ASSET)},
                },
                {
                    "key": "sht_5",
                    "type": "insert",
                    "layer": "overlay",
                    "span": words("seg_3", 5, 8),
                    "camera": {"profile_id": "desk_mirrorless", "framing": "insert", "angle": "high"},
                    "broll": {"source": "asset", "asset_id": str(BROLL_ASSET)},
                },
                {
                    "key": "sht_6",
                    "type": "title_card",
                    "layer": "overlay",
                    "span": words("seg_3", 0, 1),
                    "camera": {"profile_id": "webcam", "framing": "insert", "angle": "eye_level"},
                    "title": {"text": "On screen"},
                },
            ],
        }
    )
    data["audio"]["music"]["cues"].append(
        {
            "key": "mc_2",
            "span": {"kind": "scene", "scene_key": "scn_demo"},
            "mode": "asset",
            "asset_id": str(MUSIC_ASSET),
        }
    )
    data["audio"]["sfx"].append(
        {
            "key": "sfx_2",
            "at": {"kind": "shot", "shot_key": "sht_4", "offset_ms": 0},
            "description": "click",
            "asset_id": str(SFX_ASSET),
        }
    )
    data["captions"]["translations"] = [{"language": "de"}]
    return data


def kitchen_refs() -> BuildRefs:
    refs = example_build_refs()
    for asset_id, mime in (
        (SCREEN_ASSET, "video/mp4"),
        (MUSIC_ASSET, "audio/wav"),
        (SFX_ASSET, "audio/wav"),
        (BROLL_ASSET, "video/mp4"),
    ):
        refs.assets[asset_id] = AssetInfo(asset_id, f"{asset_id.hex:0>64}"[:64], f"k/{asset_id}", mime, 1)
    return refs


def test_example_graph_shape() -> None:
    graph = build()
    keys = [n.key for n in graph.nodes]
    assert len(keys) == len(set(keys))
    seen: set[str] = set()
    for node in graph.nodes:  # topological order
        assert set(node.deps) <= seen, node.key
        seen.add(node.key)
    by_key = graph.by_key()
    assert by_key["avatar.render:sht_1:c1:t2"].take == 2
    assert {"avatar.render:sht_1:c1:t1", "avatar.render:sht_1:c1:t2"} <= set(by_key["post.expression:sht_1"].deps)
    assert set(by_key["post.camera:sht_2"].deps) == {"video.broll:sht_2:t1", "qc.shot:sht_2:t1"}
    assert by_key["world.plate:scn_hook"].executor == "cpu"  # exact canonical plate (monitor "on" is the default state)
    assert by_key["world.plate:scn_hook"].asset_inputs == {"source": str(ALEX.PLATE_FRONT_ASSET_ID)}
    assert by_key["video.broll:sht_2:t1"].capability == "video.i2v"  # world-bound B-roll is conditioned on the plate
    for node in graph.nodes:
        if node.executor == "model":
            assert node.route is not None and node.route.adapter_id.startswith("mock_"), node.key
            assert node.estimate
    assert by_key["tts.segment:seg_1"].route == by_key["tts.segment:seg_2"].route  # one TTS route per voice
    assert by_key["qc.shot:sht_1:t1"].params["metrics"]["qc.vqa"]["adapter_id"] == "mock_qc"


def test_every_node_kind_is_constructible() -> None:
    spec = VideoSpec.model_validate(kitchen_sink_dict())
    graph = build(spec, kitchen_refs(), lipsync_patch_shots=frozenset({"sht_1"}))
    kinds = {n.kind for n in graph.nodes}
    assert kinds == set(KINDS), set(KINDS) - kinds
    by_key = graph.by_key()
    assert by_key["world.plate:scn_demo"].capability == "image.edit"  # evening: a variation of the nearest plate
    assert by_key["world.plate:scn_demo"].asset_inputs == {"base_plate": str(ALEX.PLATE_FRONT_ASSET_ID)}
    assert by_key["video.upscale:sht_1"].params["target_height"] == 1920
    assert by_key["video.interpolate:sht_1"].params["target_fps"] == 30
    assert by_key["screen.prepare:sht_4"].asset_inputs == {"source": str(SCREEN_ASSET)}
    assert not any(n.shot_key == "sht_6" for n in graph.nodes)  # title cards are drawn by render.final
    assert "captions.translate:de" in by_key


def test_graph_is_deterministic_and_content_only() -> None:
    a, b = build(), build()
    assert a == b
    data = example_spec_dict()
    data["version_id"] = str(UUID(int=7))
    data["parent_version_id"] = str(ALEX.VERSION_ID)
    other = build(VideoSpec.model_validate(data))
    assert [n.static_digest() for n in other.nodes] == [n.static_digest() for n in a.nodes]  # I5: no version ids


def test_new_record_version_with_same_content_keeps_digests() -> None:
    refs = example_build_refs()
    moved = copy.deepcopy(refs)
    new_id = UUID(int=99)
    moved.wardrobes = {new_id: replace(refs.wardrobes[ALEX.WARDROBE_VERSION_ID], version_id=new_id)}
    data = example_spec_dict()
    data["scenes"][0]["cast"][0]["wardrobe_version_id"] = str(new_id)
    graph = build(VideoSpec.model_validate(data), moved)
    assert [n.static_digest() for n in graph.nodes] == [n.static_digest() for n in build().nodes]


def test_seeds_follow_the_namespace_and_overrides() -> None:
    base = build().by_key()
    data = example_spec_dict()
    data["generation"]["seed_overrides"] = {"avatar.render:sht_1:c1:t1": 12345}
    overridden = build(VideoSpec.model_validate(data)).by_key()
    assert overridden["avatar.render:sht_1:c1:t1"].seed_base == 12345
    assert overridden["avatar.render:sht_1:c1:t2"].seed_base == base["avatar.render:sht_1:c1:t2"].seed_base
    data = example_spec_dict()
    data["generation"]["seed_namespace"] = str(UUID(int=5))
    renamed = build(VideoSpec.model_validate(data)).by_key()
    assert renamed["tts.segment:seg_1"].seed_base != base["tts.segment:seg_1"].seed_base
    assert base["avatar.render:sht_1:c1:t1"].seed_base != base["avatar.render:sht_1:c1:t2"].seed_base
    assert base["behavior.resolve:scn_hook"].seed_base is None


def test_persona_edits_do_not_touch_visual_identity_nodes() -> None:
    refs = example_build_refs()
    creator = refs.creators[ALEX.CREATOR_VERSION_ID]
    dna = copy.deepcopy(dict(creator.dna))
    dna["identity"] = {**dna["identity"], "tagline": "something new"}
    refs.creators[ALEX.CREATOR_VERSION_ID] = replace(creator, dna=dna)
    before, after = build().by_key(), build(refs=refs).by_key()
    assert after["image.keyframe:sht_1"].static_digest() == before["image.keyframe:sht_1"].static_digest()
    assert after["behavior.resolve:scn_hook"].static_digest() == before["behavior.resolve:scn_hook"].static_digest()


def test_long_shots_are_chunked_and_chunks_chain() -> None:
    data = example_spec_dict()
    text = " ".join(["word"] * 140)
    data["script"]["segments"][1]["text"] = text
    data["script"]["segments"][1]["annotations"] = []
    data["scenes"][0]["shots"][0]["span"]["end"]["word"] = 139
    data["scenes"][0]["acting"]["states"][1]["span"]["end"]["word"] = 139
    data["scenes"][0]["acting"]["events"] = []
    data["scenes"][0]["shots"][1]["span"] = words("seg_2", 2, 3)
    data["meta"]["target_duration_s"] = 60
    graph = build(VideoSpec.model_validate(data)).by_key()
    chunks = sorted(k for k in graph if k.startswith("avatar.render:sht_1:") and k.endswith(":t1"))
    assert len(chunks) >= 3
    for previous, current in itertools.pairwise(chunks):
        assert previous in graph[current].deps
    assert len({graph[k].route.adapter_id for k in graph if k.startswith("avatar.render:sht_1")}) == 1  # type: ignore[union-attr]


def test_unsupported_shot_types_raise() -> None:
    data = example_spec_dict()
    data["scenes"][0]["shots"][0]["type"] = "silent_hold"
    with pytest.raises(GraphError, match="silent_hold"):
        build(VideoSpec.model_validate(data))


def test_generation_size_keeps_aspect_and_even_dimensions() -> None:
    assert generation_size("9:16", 720) == (404, 720)
    assert generation_size("16:9", 1080) == (1920, 1080)
    assert generation_size("1:1", 540) == (540, 540)
    assert generation_size("4:5", 1350) == (1080, 1350)


def test_cache_keys_are_stable_and_complete() -> None:
    graph = build()
    node = graph.by_key()["avatar.render:sht_1:c1:t1"]
    upstream = {d: f"{i:064x}" for i, d in enumerate(node.deps)}
    cbs = "cd" * 32
    assert cache_key(node, upstream) == cache_key(build().by_key()[node.key], upstream)
    assert cache_key(node, {**upstream, node.deps[0]: "ff" * 32}) != cache_key(node, upstream)
    rerouted = node.model_copy(update={"route": node.route.model_copy(update={"revision": "2"})})  # type: ignore[union-attr]
    assert cache_key(rerouted, upstream) != cache_key(node, upstream)
    rescored = node.model_copy(update={"route": node.route.model_copy(update={"score": 0.1, "reason": "x"})})  # type: ignore[union-attr]
    assert cache_key(rescored, upstream) == cache_key(node, upstream)
    with pytest.raises(ValueError, match="upstream hashes missing"):
        cache_key(node, {})
    compile_node = graph.by_key()["behavior.compile_visual:sht_1:c1"]
    with pytest.raises(ValueError, match="CBS content digest"):
        cache_key(compile_node, {d: "00" * 32 for d in compile_node.deps})
    with_cbs = cache_key(compile_node, {d: "00" * 32 for d in compile_node.deps}, cbs)
    assert with_cbs != cache_key(compile_node, {d: "00" * 32 for d in compile_node.deps}, "ab" * 32)


def test_mock_routes_are_refused_without_mock_gpu() -> None:
    with pytest.raises(GraphError, match="MOCK_GPU"):
        build_graph(example_spec(), example_build_refs(), BUNDLE, mock_catalog(mock_gpu=False))


def test_brand_kits_enter_the_final_render_by_content() -> None:
    """Phase 12: the kit's content (colors, fonts, caption style, logo sha256) is in the
    `render.final` key; the logo is an asset input only while `brand.logo_overlay` is on."""
    from ce_build.refs import BrandKitRef

    kit_id, logo_id = UUID(int=71), UUID(int=72)
    logo = AssetInfo(logo_id, "ab" * 32, "orgs/x/logo.png", "image/png", 100, kind="logo")

    def final(overlay: bool, kit: BrandKitRef | None) -> Any:
        data = example_spec_dict()
        data["brand"] = {"brand_kit_id": str(kit_id), "logo_overlay": overlay}
        refs = example_build_refs()
        if kit is not None:
            refs.assets[logo_id] = logo
            refs.brand_kits[kit_id] = kit
        graph = build(VideoSpec.model_validate(data), refs)
        return next(n for n in graph.nodes if n.kind == "render.final")

    def key(node: Any) -> str:
        return cache_key(node, {d: "00" * 32 for d in node.deps})

    kit = BrandKitRef(kit_id, {"primary": "#112233"}, {}, None, logo)
    with pytest.raises(GraphError, match="brand kit"):
        final(True, None)
    on, off = final(True, kit), final(False, kit)
    assert on.asset_inputs == {"logo": str(logo_id)} and off.asset_inputs == {}
    assert key(on) != key(off)
    recolored = final(True, replace(kit, colors={"primary": "#445566"}))
    assert key(recolored) != key(on)
    new_logo = replace(kit, logo=replace(logo, sha256="cd" * 32))
    assert key(final(True, new_logo)) != key(on)
    same_content = replace(kit, kit_id=UUID(int=73))  # ids never enter keys (§12.2)
    assert key(final(True, same_content)) == key(on)


def test_the_room_sound_keys_on_the_camera_mic() -> None:
    """Audit OUT-ROOM: `audio.room` records through the first shot's camera-profile mic when the
    spec names none, but its key held only the acoustics — switching that camera (phone mic →
    a DSLR's lavalier) reused the cached room sound, and a cold build of the same spec differed."""
    data = example_spec_dict()
    assert data["audio"]["acoustics"].get("mic_profile") is None
    before = build(VideoSpec.model_validate(data)).by_key()
    data["scenes"][0]["shots"][0]["camera"]["profile_id"] = "dslr"
    after = build(VideoSpec.model_validate(data)).by_key()
    mics = {BUNDLE.camera_profiles[p].audio.mic_profile for p in ("phone_front_selfie", "dslr")}
    assert len(mics) == 2  # the two cameras record through different mics
    assert after["audio.room:scn_hook"].static_digest() != before["audio.room:scn_hook"].static_digest()
