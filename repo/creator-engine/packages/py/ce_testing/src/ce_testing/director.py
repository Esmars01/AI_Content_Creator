"""Director test context: the creators and worlds the acceptance fixtures plan with, without a
database. "Alex" (31, home office) is the seeded creator; "Maya" (28, bedroom, phone selfies) is a
test-only creator so the UGC example has a matching presenter and world."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from ce_build.refs import AppearanceRef, AssetInfo, BuildRefs, CreatorRef, SnapshotRef, VoiceRef, WardrobeRef, WorldRef
from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.world import WorldDNA
from ce_core.spec.videospec import VideoSpec
from ce_llm import LLMProvider, PromptLibrary
from ce_memory import MemoryRecord
from ce_memory.repetition import UsageEntry

from ce_testing.build import REPO_ROOT, config_bundle, example_build_refs, example_plates, mock_catalog
from ce_testing.fixtures import (
    ALEX,
    VOCAB_VERSION,
    _id,
    alex_appearance_dna,
    alex_creator_dna,
    alex_voice_dna,
    grey_hoodie,
    home_office_world,
)

__all__ = [
    "MAYA",
    "NOW",
    "alex_memory_records",
    "alex_option",
    "bedroom_world",
    "coverage_by_matrix",
    "director_context",
    "director_deps",
    "fixture_inputs",
    "maya_creator_dna",
    "maya_option",
    "maya_voice_dna",
    "plan_fixture",
    "refs_for",
]

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
FIXTURES_DIR = REPO_ROOT / "eval" / "llm_fixtures"


class MAYA:
    CREATOR_ID = _id("e1")
    CREATOR_VERSION_ID = _id("e2")
    APPEARANCE_VERSION_ID = _id("e3")
    VOICE_VERSION_ID = _id("e4")
    WARDROBE_VERSION_ID = _id("e5")
    WORLD_ID = _id("e6")
    WORLD_VERSION_ID = _id("e7")
    FACE_ASSET_ID = _id("e8")
    VOICE_ASSET_ID = _id("e9")
    PLATE_ASSET_ID = _id("ea")


def maya_creator_dna() -> CreatorDNA:
    data = alex_creator_dna().model_dump(mode="json")
    data["identity"] = {
        "display_name": "Maya",
        "bio": "Films honest, casual product talk and everyday routines from her bedroom.",
        "canon": [{"key": "home_city", "subject": "Maya", "predicate": "lives_in", "object": "Denver", "pinned": True}],
    }
    data["personality"]["humor_style"] = "playful"
    data["personality"]["conversational_style"] = "casual"
    data["speech"]["signature_phrases"] = [{"text": "okay so", "max_per_video": 1}]
    data["speech"]["filler_tendency"] = {"fillers": ["like"], "rate": 0.05}
    data["behavior"]["emotion_ranges"] = {"excited": [0.2, 0.9], "calm": [0.1, 0.8]}
    data["gesture"]["default_posture"] = "seated_relaxed"
    data["camera"] = {
        "framing_preference": "close_up",
        "camera_distance": "close",
        "selfie_behavior": "handheld_selfie",
        "movement_preference": "subtle_handheld",
        "preferred_camera_profiles": ["phone_front_selfie"],
    }
    data["world"] = {"world_kind_preferences": ["bedroom"], "avoid": []}
    data["avoidances"] = {"gestures": [], "emotions": [], "phrases": []}
    return CreatorDNA.model_validate(data)


def maya_voice_dna() -> VoiceDNA:
    data = alex_voice_dna().model_dump(mode="json")
    data["description"] = "bright, friendly female voice, casual American accent"
    data["references"] = [
        {"language": "en-US", "asset_id": str(MAYA.VOICE_ASSET_ID), "transcript": "Okay so, let me show you."}
    ]
    data["wpm"] = {"en": 155}
    return VoiceDNA.model_validate(data)


def bedroom_world() -> WorldDNA:
    return WorldDNA.model_validate(
        {
            "vocab_version": VOCAB_VERSION,
            "name": "Maya's bedroom",
            "kind": "bedroom",
            "style_tags": ["cozy", "bright"],
            "palette": ["#f2e6d9", "#c9a27e", "#7a9e9f"],
            "geometry": {"dimensions_m": [3.8, 3.2, 2.5], "layout": "bed against the back wall, window to the right"},
            "zones": [
                {
                    "key": "zone_bed_edge",
                    "label": "edge of the bed",
                    "position": [0.5, 0.6, 0.0],
                    "allowed_postures": ["seated_relaxed", "seated_upright", "seated_lean_in", "seated_withdrawn"],
                }
            ],
            "elements": [
                {
                    "key": "el_bed",
                    "kind": "furniture",
                    "label": "made bed with linen throw",
                    "position": [0.5, 0.8, 0.0],
                    "signature": True,
                },
                {
                    "key": "el_lamp",
                    "kind": "light",
                    "label": "warm bedside lamp",
                    "position": [0.15, 0.85, 0.6],
                    "signature": True,
                    "mutability": "stateful",
                    "states": ["lit", "unlit"],
                    "default_state": "lit",
                },
                {
                    "key": "el_window",
                    "kind": "window",
                    "label": "window with sheer curtain",
                    "position": [0.95, 0.5, 1.2],
                },
                {
                    "key": "el_shelf",
                    "kind": "decor",
                    "label": "floating shelf with plants",
                    "position": [0.4, 0.98, 1.5],
                },
            ],
            "background_layouts": {
                "cam_bed_selfie": {"visible_elements": ["el_bed", "el_lamp", "el_shelf"], "composition": "bed behind"}
            },
            "lighting": {
                "key": {"azimuth_deg": 50, "elevation_deg": 15, "intensity": 0.75, "color_temp_k": 5600},
                "fill_ratio": 0.5,
                "practicals": [{"element": "el_lamp", "color_temp_k": 2700}],
                "tolerance": {"color_temp_k": 400, "luminance": 0.12},
            },
            "time_and_weather": {
                "default_time_of_day": "morning",
                "allowed_times": ["morning", "late_afternoon", "evening"],
                "default_weather": "clear",
                "allowed_weather": ["clear", "overcast"],
            },
            "acoustics": {
                "room_profile": "small_bedroom",
                "rt60_s": 0.3,
                "ambient": ["room_tone_quiet"],
                "noise_floor_db": -64,
            },
            "camera_positions": [
                {
                    "key": "cam_bed_selfie",
                    "height_m": 1.0,
                    "distance_m": 0.45,
                    "lens_equiv_mm": 24,
                    "default_framing": "close_up",
                    "allowed_camera_profiles": ["phone_front_selfie", "phone_rear_handheld"],
                    "status": "permitted",
                }
            ],
            "continuity": {"must_show_from": {"cam_bed_selfie": ["el_bed"]}},
        }
    )


def alex_memory_records() -> list[MemoryRecord]:
    """The seeded memory items (`ce_testing.seed`) as retrieval records."""
    rows: list[tuple[UUID, str, dict[str, Any], str, bool]] = [
        (
            ALEX.MEMORY_GAZE_ID,
            "gaze_habit.thinking_glance",
            {"direction": "down_left", "typical_ms": 600},
            "Glances down-left for about 600 ms while thinking.",
            False,
        ),
        (
            ALEX.MEMORY_LAUGH_ID,
            "reaction_habit.laughter",
            {"expression": "small_smile", "intensity": 0.3, "frequency": "often"},
            "Usually laughs with a small smile.",
            False,
        ),
        (
            ALEX.MEMORY_PHRASE_ID,
            "speech_habit.recurring_phrase",
            {"text": "here's the thing", "max_per_video": 1, "contexts": ["reveal"]},
            "Says 'here's the thing' before a reveal.",
            False,
        ),
        (
            ALEX.MEMORY_FACT_ID,
            "persona_fact",
            {"subject": "Alex", "predicate": "lives_in", "object": "Austin"},
            "Alex lives in Austin.",
            True,
        ),
    ]
    return [
        MemoryRecord(
            id=item_id,
            category=kind.split(".")[0],
            kind=kind,
            key=f"{kind}:{i}",
            value=value,
            text=text,
            confidence=1.0,
            last_seen_at=NOW,
            pinned=pinned,
        )
        for i, (item_id, kind, value, text, pinned) in enumerate(rows)
    ]


def alex_option(*, memory: Sequence[MemoryRecord] | None = None, usage: Sequence[UsageEntry] = ()) -> Any:
    from ce_director import CreatorOption

    return CreatorOption(
        creator_id=ALEX.CREATOR_ID,
        creator_version_id=ALEX.CREATOR_VERSION_ID,
        dna=alex_creator_dna(),
        voice_version_id=ALEX.VOICE_VERSION_ID,
        voice=alex_voice_dna(),
        appearance_version_id=ALEX.APPEARANCE_VERSION_ID,
        appearance_age=alex_appearance_dna().age_appearance,
        appearance_text="short dark brown hair, light stubble, man",
        wardrobe_version_ids=[ALEX.WARDROBE_VERSION_ID],
        default_world_ids=[ALEX.WORLD_ID],
        memory=list(alex_memory_records() if memory is None else memory),
        version_numbers={ALEX.CREATOR_VERSION_ID: 1},
        recent_usage=list(usage),
    )


def maya_option() -> Any:
    from ce_director import CreatorOption

    return CreatorOption(
        creator_id=MAYA.CREATOR_ID,
        creator_version_id=MAYA.CREATOR_VERSION_ID,
        dna=maya_creator_dna(),
        voice_version_id=MAYA.VOICE_VERSION_ID,
        voice=maya_voice_dna(),
        appearance_version_id=MAYA.APPEARANCE_VERSION_ID,
        appearance_age=28,
        appearance_text="long auburn hair, freckles, woman",
        wardrobe_version_ids=[MAYA.WARDROBE_VERSION_ID],
        default_world_ids=[MAYA.WORLD_ID],
        version_numbers={MAYA.CREATOR_VERSION_ID: 1},
    )


def director_context(
    *,
    video_id: UUID | None = None,
    version_id: UUID | None = None,
    creators: Sequence[Any] | None = None,
    pinned: Mapping[UUID, tuple[UUID, list[dict[str, object]]]] | None = None,
    facts: Sequence[Any] = (),
) -> Any:
    from ce_director import DirectorContext, WorldOption

    return DirectorContext(
        org_id=ALEX.ORG_ID,
        video_id=video_id or _id("d1"),
        version_id=version_id or _id("d2"),
        now=NOW,
        creators=list(creators) if creators is not None else [alex_option(), maya_option()],
        worlds=[
            WorldOption(ALEX.WORLD_ID, ALEX.WORLD_VERSION_ID, home_office_world(), frozenset({"cam_desk_front"})),
            WorldOption(MAYA.WORLD_ID, MAYA.WORLD_VERSION_ID, bedroom_world(), frozenset({"cam_bed_selfie"})),
        ],
        pinned_snapshots=dict(pinned or {}),
        facts=list(facts),
        source_ids=sorted({f.source_id for f in facts}, key=str),
    )


def _asset(asset_id: UUID, label: str, mime: str) -> AssetInfo:
    return AssetInfo(
        asset_id=asset_id,
        sha256=hashlib.sha256(label.encode()).hexdigest(),
        storage_key=f"test/{label}",
        mime=mime,
        bytes=1024,
        kind="audio" if mime.startswith("audio/") else "reference",
    )


async def refs_for(spec: VideoSpec, snapshots: Mapping[UUID, SnapshotRef]) -> BuildRefs:
    """BuildRefs for a planned spec: Alex's seeded records, Maya's test records and the new snapshots."""
    refs = example_build_refs()
    face = _asset(MAYA.FACE_ASSET_ID, "maya-face.png", "image/png")
    voice_ref = _asset(MAYA.VOICE_ASSET_ID, "maya-voice.wav", "audio/wav")
    plate = _asset(MAYA.PLATE_ASSET_ID, "maya-bedroom-plate.png", "image/png")
    for asset in (face, voice_ref, plate):
        refs.assets[asset.asset_id] = asset
    refs.creators[MAYA.CREATOR_VERSION_ID] = CreatorRef.from_dna(
        MAYA.CREATOR_VERSION_ID, MAYA.CREATOR_ID, maya_creator_dna(), MAYA.APPEARANCE_VERSION_ID, MAYA.VOICE_VERSION_ID
    )
    appearance = alex_appearance_dna().model_copy(update={"age_appearance": 28, "hair": "long auburn"})
    refs.appearances[MAYA.APPEARANCE_VERSION_ID] = AppearanceRef(
        MAYA.APPEARANCE_VERSION_ID, appearance.model_dump(mode="json"), face
    )
    refs.voices[MAYA.VOICE_VERSION_ID] = VoiceRef.from_dna(MAYA.VOICE_VERSION_ID, maya_voice_dna(), refs.assets)
    refs.wardrobes[MAYA.WARDROBE_VERSION_ID] = WardrobeRef(
        MAYA.WARDROBE_VERSION_ID,
        grey_hoodie().model_copy(update={"name": "oversized cardigan"}).model_dump(mode="json"),
    )
    refs.worlds[MAYA.WORLD_VERSION_ID] = WorldRef.from_dna(
        MAYA.WORLD_VERSION_ID,
        bedroom_world(),
        {"cam_bed_selfie": {"morning": {"clear": str(plate.asset_id)}}},
        refs.assets,
    )
    refs.worlds[ALEX.WORLD_VERSION_ID] = WorldRef.from_dna(
        ALEX.WORLD_VERSION_ID, home_office_world(), example_plates(), refs.assets
    )
    refs.snapshots.update(snapshots)
    return refs


def director_deps(provider: LLMProvider | None = None, *, fixtures_dir: Path | None = None, **changes: Any) -> Any:
    """Director dependencies over the test config and the mock router catalog; with no provider
    given, the fixture provider replays `eval/llm_fixtures` (or `fixtures_dir`)."""
    from ce_director import DirectorDeps
    from ce_llm import create_llm_provider

    bundle = config_bundle()
    if provider is None:
        provider = create_llm_provider(
            "fixture", app_env="test", config={"fixtures_dir": str(fixtures_dir or FIXTURES_DIR)}
        )
    return DirectorDeps(
        bundle=bundle,
        catalog=mock_catalog(bundle),
        prompts=PromptLibrary(REPO_ROOT / "prompts"),
        provider=provider,
        refs_for=refs_for,
        **changes,
    )


# ---------------------------------------------------------------------- planning helpers for tests


def fixture_inputs(name: str) -> list[str]:
    import yaml

    doc = yaml.safe_load((FIXTURES_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    return [str(i) for i in doc["inputs"]]


_PLANS: dict[tuple[str, int], Any] = {}


def plan_fixture(name: str, index: int = 0) -> Any:
    """The Director's plan for input `index` of fixture `name` (cached per test session)."""
    import asyncio

    from ce_director import Director, PlanRequest

    key = (name, index)
    if key not in _PLANS:
        director = Director(director_deps())
        request = PlanRequest(input=fixture_inputs(name)[index])
        _PLANS[key] = asyncio.run(director.plan(request, director_context()))
    return _PLANS[key]


def coverage_by_matrix(outcome: Any) -> dict[str, Any]:
    """Plan-time CBS, compile and predicted coverage of a planned version on each mock matrix
    (`global` = mock_avatar_global only, `segment` = mock_avatar_segment only), persisting nothing."""
    import asyncio

    from ce_behavior.plan import compile_version, predicted_coverage, version_cbs
    from ce_build import build_graph

    from ce_testing.behavior import GLOBAL_ONLY, SEGMENT_ONLY, catalog_with

    bundle = config_bundle()
    spec = outcome.spec
    snapshots = {
        sid: SnapshotRef(sid, draft.digest(), tuple(i.model_dump(mode="json") for i in draft.items))
        for sid, draft in outcome.snapshots
    }
    refs = asyncio.run(refs_for(spec, snapshots))
    mode = bundle.modes[str(spec.meta.mode)]
    out: dict[str, Any] = {}
    for name, disabled in (("global", GLOBAL_ONLY), ("segment", SEGMENT_ONLY)):
        catalog = catalog_with(disabled)
        graph = build_graph(spec, refs, bundle, catalog)
        routes = {n.key: n.route for n in graph.nodes if n.route is not None}
        cbs = version_cbs(spec, refs, bundle.vocab, bundle.app.behavior)
        compiled = compile_version(
            spec,
            cbs,
            routes,
            catalog.manifests,
            bundle.vocab,
            editorial_methods=frozenset(mode.editorial_methods),
            stage="plan_time",
        )
        out[name] = {"cbs": cbs, "report": predicted_coverage(cbs, compiled, bundle.vocab, stage="predicted")}
    return out
