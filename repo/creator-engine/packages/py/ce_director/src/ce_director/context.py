"""Director stage 2 (§13): the context a plan is made in.

`DirectorContext` holds what the database offers — the org's approved creators (DNA, voice,
appearance age, default wardrobes and worlds), approved worlds, each creator's memory records and
recent usage — and is built by `ce_director.store.load_context` or, in tests, from fixtures.
`ContextPack` is the part chosen for one plan: the cast member, the world options, the pinned
MemorySnapshot (I7: memory reaches planning only through it; the whole memory is never sent to
the LLM) and the recent usage log for the repetition guard.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.world import WorldDNA
from ce_memory import MemoryRecord, SnapshotDraft
from ce_memory.repetition import UsageEntry
from ce_research.ingest import StoredFact

__all__ = ["BrandDefault", "ContextPack", "CreatorOption", "DirectorContext", "WorldOption", "choose_creator"]

_FEMALE = re.compile(r"\b(female|woman|women|she|her|girl|feminine)\b", re.IGNORECASE)
_MALE = re.compile(r"\b(male|man|men|he|his|guy|boy|masculine)\b", re.IGNORECASE)


@dataclass(frozen=True)
class WorldOption:
    world_id: UUID
    world_version_id: UUID
    dna: WorldDNA
    plate_positions: frozenset[str] = frozenset()  # camera positions with an approved plate


@dataclass
class CreatorOption:
    creator_id: UUID
    creator_version_id: UUID
    dna: CreatorDNA
    voice_version_id: UUID | None
    voice: VoiceDNA | None
    appearance_version_id: UUID | None
    appearance_age: int | None = None
    appearance_text: str = ""
    wardrobe_version_ids: list[UUID] = field(default_factory=list)
    default_world_ids: list[UUID] = field(default_factory=list)
    memory: list[MemoryRecord] = field(default_factory=list)
    version_numbers: dict[UUID, int] = field(default_factory=dict)
    recent_usage: list[UsageEntry] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.dna.identity.display_name

    @property
    def gender(self) -> str:
        """A heuristic from the voice and appearance descriptions, used only to match cast hints."""
        text = " ".join([self.voice.description if self.voice else "", self.appearance_text])
        if _FEMALE.search(text):
            return "female"
        if _MALE.search(text):
            return "male"
        return "unspecified"

    def wpm(self, language: str, default: float) -> float:
        return self.voice.wpm_for(language, default) if self.voice else default


@dataclass(frozen=True)
class BrandDefault:
    """The brand a new plan starts with (Phase 12): the project's brand kit, or a replanned
    version's own brand. The kit's caption style is used unless the request names one."""

    brand_kit_id: UUID | None
    logo_overlay: bool = False
    caption_style_id: str | None = None


@dataclass
class DirectorContext:
    org_id: UUID
    video_id: UUID
    version_id: UUID
    now: datetime
    creators: list[CreatorOption]
    worlds: list[WorldOption]
    # Replan: the snapshots pinned on the version being replanned, reused unless refreshed (§18.5):
    # creator_version_id → (snapshot id, its copied items).
    pinned_snapshots: dict[UUID, tuple[UUID, list[dict[str, object]]]] = field(default_factory=dict)
    # Phase 12: facts of the persistent research sources the request attached (data, I10)
    facts: list[StoredFact] = field(default_factory=list)
    source_ids: list[UUID] = field(default_factory=list)
    brand: BrandDefault | None = None

    def world(self, world_version_id: UUID) -> WorldOption | None:
        return next((w for w in self.worlds if w.world_version_id == world_version_id), None)


@dataclass
class ContextPack:
    creator: CreatorOption
    character_key: str
    worlds: list[WorldOption]
    snapshot_id: UUID
    snapshot: SnapshotDraft | None  # None when a pinned snapshot is reused (it already exists)
    snapshot_items: list[dict[str, object]]
    recent_usage: list[UsageEntry]
    assumptions: list[str] = field(default_factory=list)


def choose_creator(
    options: Sequence[CreatorOption], *, requested: UUID | None, age: int | None, gender: str
) -> tuple[CreatorOption, list[str]]:
    """The requested creator, else the closest match to the cast hints (deterministic)."""
    if not options:
        raise LookupError("no approved creator is available for planning")
    notes: list[str] = []
    if requested is not None:
        chosen = next((o for o in options if o.creator_id == requested), None)
        if chosen is None:
            raise LookupError(f"creator {requested} has no approved version in this org")
        return chosen, notes

    def distance(option: CreatorOption) -> tuple[int, int, str]:
        gender_miss = int(gender != "unspecified" and option.gender not in (gender, "unspecified"))
        age_gap = abs((option.appearance_age or age or 0) - (age or option.appearance_age or 0)) if age else 0
        return (gender_miss, age_gap, str(option.creator_id))

    chosen = min(options, key=distance)
    gender_miss, age_gap, _ = distance(chosen)
    wanted = " ".join(x for x in (f"about {age}" if age else "", gender if gender != "unspecified" else "") if x)
    if wanted and (gender_miss or age_gap > 3):
        notes.append(
            f"No approved creator matches the requested presenter ({wanted}); using {chosen.name} "
            f"(appears {chosen.appearance_age or 'unknown age'}, {chosen.gender}), the closest match."
        )
    elif wanted:
        notes.append(f"Cast {chosen.name} as the closest approved creator to the request ({wanted}).")
    return chosen, notes
