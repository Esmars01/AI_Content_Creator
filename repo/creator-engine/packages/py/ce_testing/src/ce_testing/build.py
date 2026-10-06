"""Build fixtures: the example spec's resolved references (`BuildRefs`) and a mock router catalog.

The asset digests are those of the dev seed's placeholder objects, so a graph built here has the
same cache keys as one the orchestrator builds from the seeded database.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from ce_build.refs import (
    AppearanceRef,
    AssetInfo,
    BuildRefs,
    CreatorRef,
    SnapshotRef,
    VoiceRef,
    WardrobeRef,
    WorldRef,
)
from ce_config.loader import ConfigBundle, load_config
from ce_contracts.plugins import discover
from ce_policy import OperatorProfile
from ce_router import RouterCatalog, build_catalog

from ce_testing.fixtures import (
    ALEX,
    alex_appearance_dna,
    alex_creator_dna,
    alex_memory_snapshot,
    alex_voice_dna,
    alex_voice_uk_dna,
    grey_hoodie,
    home_office_world,
    modern_office_world,
    navy_sweater,
)
from ce_testing.seed import placeholder_objects

__all__ = ["REPO_ROOT", "config_bundle", "example_build_refs", "example_plates", "mock_catalog", "office_plates"]

REPO_ROOT = Path(__file__).resolve().parents[5]


def example_plates() -> dict[str, dict[str, dict[str, str]]]:
    """`world_versions.plates` of the seeded home office (v1)."""
    return {
        "cam_desk_front": {"late_afternoon": {"clear": str(ALEX.PLATE_FRONT_ASSET_ID)}},
        "cam_side_wide": {"late_afternoon": {"clear": str(ALEX.PLATE_SIDE_ASSET_ID)}},
    }


def office_plates() -> dict[str, dict[str, dict[str, str]]]:
    """`world_versions.plates` of the seeded modern office."""
    return {
        "cam_desk_front": {"midday": {"clear": str(ALEX.PLATE_OFFICE_FRONT_ASSET_ID)}},
        "cam_wide": {"midday": {"clear": str(ALEX.PLATE_OFFICE_WIDE_ASSET_ID)}},
    }


def example_build_refs() -> BuildRefs:
    """The example spec's references plus the seeded edit fixtures (the British-accent voice
    version, the navy sweater and the modern office)."""
    assets = {
        o.asset_id: AssetInfo(
            asset_id=o.asset_id,
            sha256=hashlib.sha256(o.data).hexdigest(),
            storage_key=o.key,
            mime=o.mime,
            bytes=len(o.data),
            kind="audio" if o.mime.startswith("audio/") else "reference",
        )
        for o in placeholder_objects()
    }
    voice = VoiceRef.from_dna(ALEX.VOICE_VERSION_ID, alex_voice_dna(), assets)
    voice_uk = VoiceRef.from_dna(ALEX.VOICE_UK_VERSION_ID, alex_voice_uk_dna(), assets)
    return BuildRefs(
        creators={
            ALEX.CREATOR_VERSION_ID: CreatorRef.from_dna(
                ALEX.CREATOR_VERSION_ID,
                ALEX.CREATOR_ID,
                alex_creator_dna(),
                ALEX.APPEARANCE_VERSION_ID,
                ALEX.VOICE_VERSION_ID,
            )
        },
        appearances={
            ALEX.APPEARANCE_VERSION_ID: AppearanceRef(
                ALEX.APPEARANCE_VERSION_ID,
                alex_appearance_dna().model_dump(mode="json"),
                assets[ALEX.CANONICAL_FACE_ASSET_ID],
            )
        },
        voices={ALEX.VOICE_VERSION_ID: voice, ALEX.VOICE_UK_VERSION_ID: voice_uk},
        wardrobes={
            ALEX.WARDROBE_VERSION_ID: WardrobeRef(ALEX.WARDROBE_VERSION_ID, grey_hoodie().model_dump(mode="json")),
            ALEX.WARDROBE_NAVY_VERSION_ID: WardrobeRef(
                ALEX.WARDROBE_NAVY_VERSION_ID, navy_sweater().model_dump(mode="json")
            ),
        },
        worlds={
            ALEX.WORLD_VERSION_ID: WorldRef.from_dna(
                ALEX.WORLD_VERSION_ID, home_office_world(), example_plates(), assets
            ),
            ALEX.OFFICE_WORLD_VERSION_ID: WorldRef.from_dna(
                ALEX.OFFICE_WORLD_VERSION_ID, modern_office_world(), office_plates(), assets
            ),
        },
        snapshots={ALEX.SNAPSHOT_ID: SnapshotRef.from_snapshot(alex_memory_snapshot())},
        assets=assets,
    )


@cache
def config_bundle() -> ConfigBundle:
    return load_config(REPO_ROOT / "config", "test")


def mock_catalog(bundle: ConfigBundle | None = None, **changes: object) -> RouterCatalog:
    """The router catalog of a dev/test deployment with MOCK_GPU=true (mocks only)."""
    from dataclasses import replace

    bundle = bundle or config_bundle()
    registry = discover(app_env="test", include_mocks=True)
    catalog = build_catalog(registry, bundle, app_env="test", mock_gpu=True, operator=OperatorProfile())
    return replace(catalog, **changes) if changes else catalog  # type: ignore[arg-type]
