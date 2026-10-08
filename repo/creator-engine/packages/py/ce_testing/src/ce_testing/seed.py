"""The dev seed (§40 Phase 1, `ce seed dev`): an org, a platform-admin user, creator "Alex"
(approved version with DNA and canon, an appearance with a placeholder identity pack, a
designed voice with default WPM), the wardrobe "grey hoodie", the world "Alex's home office"
v1 with placeholder plates for cam_desk_front and cam_side_wide, and a few authored memory items.

Idempotent: running it twice leaves one copy. Placeholder assets are recorded as such; no
model produced them (rule 5). The org is flagged `is_demo`: production refuses to sign into it, and
`ce data purge-demo` removes it with everything it holds.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from ce_core.canonical import content_digest
from ce_core.identity.memory import dedup_key, value_hash
from ce_core.vocab import Vocabulary
from ce_db.models import assets, creators, memory, tenancy, videos, worlds
from ce_storage.keys import asset_key
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ce_testing.fixtures import (
    ALEX,
    alex_appearance_dna,
    alex_creator_dna,
    alex_memory_snapshot,
    alex_voice_dna,
    alex_voice_uk_dna,
    example_spec,
    grey_hoodie,
    home_office_world,
    modern_office_world,
    navy_sweater,
)
from ce_testing.placeholders import placeholder_png, placeholder_wav

__all__ = [
    "DEV_ADMIN_EMAIL",
    "PlaceholderObject",
    "SeedResult",
    "example_version_row",
    "placeholder_objects",
    "seed_dev",
]

DEV_ADMIN_EMAIL = "admin@creator-engine.local"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)

SeedResult = dict[str, UUID]


async def _upsert(session: AsyncSession, table: Any, rows: list[dict[str, Any]]) -> None:
    pk = [c.name for c in table.primary_key.columns]
    await session.execute(insert(table).values(rows).on_conflict_do_nothing(index_elements=pk))


@dataclass(frozen=True)
class PlaceholderObject:
    """A placeholder file the seed uploads to the assets bucket under `key`."""

    asset_id: UUID
    key: str
    data: bytes
    mime: str


def placeholder_objects(org_id: UUID = ALEX.ORG_ID) -> list[PlaceholderObject]:
    def obj(asset_id: UUID, data: bytes, mime: str) -> PlaceholderObject:
        return PlaceholderObject(asset_id, asset_key(org_id, asset_id), data, mime)

    return [
        obj(ALEX.CANONICAL_FACE_ASSET_ID, placeholder_png("alex_canonical_face"), "image/png"),
        obj(ALEX.VOICE_REFERENCE_ASSET_ID, placeholder_wav(), "audio/wav"),
        obj(ALEX.PLATE_FRONT_ASSET_ID, placeholder_png("home_office_cam_desk_front", 96, 54), "image/png"),
        obj(ALEX.PLATE_SIDE_ASSET_ID, placeholder_png("home_office_cam_side_wide", 96, 54), "image/png"),
        obj(ALEX.PLATE_OFFICE_FRONT_ASSET_ID, placeholder_png("modern_office_cam_desk_front", 96, 54), "image/png"),
        obj(ALEX.PLATE_OFFICE_WIDE_ASSET_ID, placeholder_png("modern_office_cam_wide", 96, 54), "image/png"),
    ]


def _placeholder_asset(item: PlaceholderObject) -> dict[str, Any]:
    return {
        "id": item.asset_id,
        "org_id": ALEX.ORG_ID,
        "kind": "audio" if item.mime.startswith("audio/") else "reference",
        "storage_key": item.key,
        "mime": item.mime,
        "bytes": len(item.data),
        "sha256": hashlib.sha256(item.data).hexdigest(),
        "probe": {"placeholder": True},
        "rights": {"owner": "seed", "license": "project", "placeholder": True},
        "status": "ready",
        "tags": ["placeholder", "seed"],
    }


async def seed_dev(session: AsyncSession, vocab: Vocabulary, password_hash: str | None = None) -> SeedResult:
    org = ALEX.ORG_ID
    await _upsert(
        session, tenancy.Organization.__table__, [{"id": org, "name": "Dev Org", "plan": "dev", "is_demo": True}]
    )
    # An org seeded before the provenance flag existed is still the seed's (same fixed id).
    await session.execute(sa.update(tenancy.Organization).where(tenancy.Organization.id == org).values(is_demo=True))
    await _upsert(
        session,
        tenancy.User.__table__,
        [
            {
                "id": ALEX.USER_ID,
                "email": DEV_ADMIN_EMAIL,
                "name": "Dev Admin",
                "password_hash": password_hash,
                "is_platform_admin": True,
            }
        ],
    )
    await _upsert(session, tenancy.Membership.__table__, [{"user_id": ALEX.USER_ID, "org_id": org, "role": "owner"}])
    await _upsert(session, tenancy.OperatorProfile.__table__, [{"org_id": org, "regions_served": ["EU"]}])
    await _upsert(session, videos.Project.__table__, [{"id": ALEX.PROJECT_ID, "org_id": org, "name": "Dev project"}])
    await _upsert(
        session,
        assets.Asset.__table__,
        [_placeholder_asset(item) for item in placeholder_objects(org)],
    )

    # Creator root, then its versions (current_version_id is set once versions exist).
    await _upsert(session, creators.Creator.__table__, [{"id": ALEX.CREATOR_ID, "org_id": org, "name": "Alex"}])
    await _upsert(
        session,
        creators.Appearance.__table__,
        [{"id": ALEX.APPEARANCE_ID, "org_id": org, "creator_id": ALEX.CREATOR_ID, "name": "Alex default look"}],
    )
    await _upsert(
        session,
        creators.AppearanceVersion.__table__,
        [
            {
                "id": ALEX.APPEARANCE_VERSION_ID,
                "org_id": org,
                "appearance_id": ALEX.APPEARANCE_ID,
                "number": 1,
                "dna": alex_appearance_dna().model_dump(mode="json"),
                "canonical_face_asset_id": ALEX.CANONICAL_FACE_ASSET_ID,
                "identity_pack": {"placeholder": True, "images": []},
                "age_checks": {
                    "age_appearance": 31,
                    "owner_attestation": "seed",
                    "vlm_estimate": None,
                    "note": "placeholder identity pack; the VLM age check runs with BuildIdentityPackWorkflow",
                },
                "status": "approved",
            }
        ],
    )
    await _upsert(
        session,
        creators.Voice.__table__,
        [{"id": ALEX.VOICE_ID, "org_id": org, "creator_id": ALEX.CREATOR_ID, "name": "Alex voice", "kind": "designed"}],
    )
    voice = alex_voice_dna()
    await _upsert(
        session,
        creators.VoiceVersion.__table__,
        [
            {
                "id": ALEX.VOICE_VERSION_ID,
                "org_id": org,
                "voice_id": ALEX.VOICE_ID,
                "number": 1,
                "references": [r.model_dump(mode="json") for r in voice.references],
                "description": voice.description,
                "wpm": voice.wpm,
                "lexicon": [e.model_dump(mode="json") for e in voice.lexicon],
                "default_prosody": voice.default_prosody.model_dump(mode="json"),
                "status": "approved",
            }
        ],
    )
    uk = alex_voice_uk_dna()
    await _upsert(
        session,
        creators.VoiceVersion.__table__,
        [
            {
                "id": ALEX.VOICE_UK_VERSION_ID,
                "org_id": org,
                "voice_id": ALEX.VOICE_ID,
                "number": 2,
                "parent_version_id": ALEX.VOICE_VERSION_ID,
                "references": [r.model_dump(mode="json") for r in uk.references],
                "description": uk.description,
                "wpm": uk.wpm,
                "lexicon": [e.model_dump(mode="json") for e in uk.lexicon],
                "default_prosody": uk.default_prosody.model_dump(mode="json"),
                "status": "approved",
            }
        ],
    )
    await _upsert(
        session,
        creators.Wardrobe.__table__,
        [
            {"id": ALEX.WARDROBE_ID, "org_id": org, "creator_id": ALEX.CREATOR_ID, "name": "grey hoodie"},
            {"id": ALEX.WARDROBE_NAVY_ID, "org_id": org, "creator_id": ALEX.CREATOR_ID, "name": "navy sweater"},
        ],
    )
    await _upsert(
        session,
        creators.WardrobeVersion.__table__,
        [
            {
                "id": ALEX.WARDROBE_VERSION_ID,
                "org_id": org,
                "wardrobe_id": ALEX.WARDROBE_ID,
                "number": 1,
                "spec": grey_hoodie().model_dump(mode="json"),
                "status": "approved",
            },
            {
                "id": ALEX.WARDROBE_NAVY_VERSION_ID,
                "org_id": org,
                "wardrobe_id": ALEX.WARDROBE_NAVY_ID,
                "number": 1,
                "spec": navy_sweater().model_dump(mode="json"),
                "status": "approved",
            },
        ],
    )
    await _upsert(
        session,
        worlds.World.__table__,
        [
            {
                "id": ALEX.WORLD_ID,
                "org_id": org,
                "name": "Alex's home office",
                "kind": "home_office",
                "owner_creator_id": ALEX.CREATOR_ID,
            },
            {
                "id": ALEX.OFFICE_WORLD_ID,
                "org_id": org,
                "name": "Modern office",
                "kind": "office",
                "owner_creator_id": None,
            },
        ],
    )
    plates = {
        "cam_desk_front": {"late_afternoon": {"clear": str(ALEX.PLATE_FRONT_ASSET_ID)}},
        "cam_side_wide": {"late_afternoon": {"clear": str(ALEX.PLATE_SIDE_ASSET_ID)}},
    }
    await _upsert(
        session,
        worlds.WorldVersion.__table__,
        [
            {
                "id": ALEX.WORLD_VERSION_ID,
                "org_id": org,
                "world_id": ALEX.WORLD_ID,
                "number": 1,
                "dna": home_office_world().model_dump(mode="json"),
                "plates": plates,
                "plate_candidates": {"placeholder": True},
                "status": "approved",
                "approved_at": NOW,
            },
            {
                "id": ALEX.OFFICE_WORLD_VERSION_ID,
                "org_id": org,
                "world_id": ALEX.OFFICE_WORLD_ID,
                "number": 1,
                "dna": modern_office_world().model_dump(mode="json"),
                "plates": {
                    "cam_desk_front": {"midday": {"clear": str(ALEX.PLATE_OFFICE_FRONT_ASSET_ID)}},
                    "cam_wide": {"midday": {"clear": str(ALEX.PLATE_OFFICE_WIDE_ASSET_ID)}},
                },
                "plate_candidates": {"placeholder": True},
                "status": "approved",
                "approved_at": NOW,
            },
        ],
    )
    await _upsert(
        session,
        creators.CreatorVersion.__table__,
        [
            {
                "id": ALEX.CREATOR_VERSION_ID,
                "org_id": org,
                "creator_id": ALEX.CREATOR_ID,
                "number": 1,
                "dna": alex_creator_dna().model_dump(mode="json"),
                "appearance_version_id": ALEX.APPEARANCE_VERSION_ID,
                "voice_version_id": ALEX.VOICE_VERSION_ID,
                "default_world_ids": [ALEX.WORLD_ID],
                "default_wardrobe_version_ids": [ALEX.WARDROBE_VERSION_ID],
                "status": "approved",
                "approved_at": NOW,
            }
        ],
    )
    pairs: list[tuple[Any, UUID, UUID]] = [
        (creators.Creator.__table__, ALEX.CREATOR_ID, ALEX.CREATOR_VERSION_ID),
        (creators.Appearance.__table__, ALEX.APPEARANCE_ID, ALEX.APPEARANCE_VERSION_ID),
        (creators.Voice.__table__, ALEX.VOICE_ID, ALEX.VOICE_VERSION_ID),
        (creators.Wardrobe.__table__, ALEX.WARDROBE_ID, ALEX.WARDROBE_VERSION_ID),
        (worlds.World.__table__, ALEX.WORLD_ID, ALEX.WORLD_VERSION_ID),
        (creators.Wardrobe.__table__, ALEX.WARDROBE_NAVY_ID, ALEX.WARDROBE_NAVY_VERSION_ID),
        (worlds.World.__table__, ALEX.OFFICE_WORLD_ID, ALEX.OFFICE_WORLD_VERSION_ID),
    ]
    for table, row_id, version_id in pairs:
        await session.execute(
            sa.update(table)
            .where(table.c.id == row_id, table.c.current_version_id.is_(None))
            .values(current_version_id=version_id)
        )

    # A few authored memory items (active, confidence 1, §18.4) and the snapshot the example spec pins.
    authored: list[tuple[UUID, str, dict[str, Any], str]] = [
        (
            ALEX.MEMORY_GAZE_ID,
            "gaze_habit.thinking_glance",
            {"direction": "down_left", "typical_ms": 600},
            "Glances down-left for about 600 ms while thinking.",
        ),
        (
            ALEX.MEMORY_LAUGH_ID,
            "reaction_habit.laughter",
            {"expression": "small_smile", "intensity": 0.3, "frequency": "often"},
            "Usually laughs with a small smile.",
        ),
        (
            ALEX.MEMORY_PHRASE_ID,
            "speech_habit.recurring_phrase",
            {"text": "here's the thing", "max_per_video": 1, "contexts": ["reveal"]},
            "Says 'here's the thing' before a reveal.",
        ),
        (
            ALEX.MEMORY_FACT_ID,
            "persona_fact",
            {"subject": "Alex", "predicate": "lives_in", "object": "Austin"},
            "Alex lives in Austin.",
        ),
    ]
    rows = []
    for item_id, kind, value, text in authored:
        typed = vocab.validate_memory_value(kind, value).model_dump(mode="json")
        rows.append(
            {
                "id": item_id,
                "org_id": org,
                "creator_id": ALEX.CREATOR_ID,
                "category": kind.split(".")[0],
                "kind": kind,
                "key": dedup_key(vocab, kind, typed),
                "value_hash": value_hash(typed),
                "value": typed,
                "text": text,
                "vocab_version": vocab.version,
                "source": {"type": "authored"},
                "confidence": 1.0,
                "status": "active",
                "pinned": kind == "persona_fact",
                "first_seen_at": NOW,
                "last_seen_at": NOW,
            }
        )
    await _upsert(session, memory.CreatorMemoryItem.__table__, rows)
    snapshot = alex_memory_snapshot()
    await _upsert(
        session,
        memory.MemorySnapshot.__table__,
        [
            {
                "id": snapshot.id,
                "org_id": org,
                "creator_version_id": snapshot.creator_version_id,
                "items": [i.model_dump(mode="json") for i in snapshot.items],
                "params": snapshot.retrieval_params,
                "conflicts": [],
                "digest": snapshot.digest(),
            }
        ],
    )
    await session.flush()
    return {
        "org_id": org,
        "user_id": ALEX.USER_ID,
        "project_id": ALEX.PROJECT_ID,
        "creator_id": ALEX.CREATOR_ID,
        "creator_version_id": ALEX.CREATOR_VERSION_ID,
        "world_version_id": ALEX.WORLD_VERSION_ID,
        "snapshot_id": snapshot.id,
    }


def example_version_row(org_id: UUID = ALEX.ORG_ID) -> dict[str, Any]:
    """A `video_versions` row for the §11 example spec (the projection and API tests use it)."""
    spec = example_spec()
    return {
        "id": spec.version_id,
        "org_id": org_id,
        "video_id": spec.video_id,
        "number": 1,
        "spec": spec.model_dump(mode="json"),
        "spec_hash": content_digest(spec.model_dump(mode="json")),
        "spec_content_digest": spec.content_digest(),
        "state": "planned",
        "origin": "plan",
    }
