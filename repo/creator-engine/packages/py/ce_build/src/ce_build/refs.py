"""The approved records a spec references, resolved for the build (§12.2 "referenced DNA, world and
memory fields, restricted to the fields the node reads").

The orchestrator loads them from the database; tests build them from fixtures. Digests here are
content digests of exactly the fields a node kind reads, so a change elsewhere in a DNA never
invalidates that node (e.g. persona traits never touch `image.keyframe`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from ce_core.canonical import content_digest
from ce_core.identity.creator import CreatorDNA, VoiceDNA
from ce_core.identity.memory import MemorySnapshot
from ce_core.identity.world import WorldDNA

__all__ = [
    "AppearanceRef",
    "AssetInfo",
    "BrandKitRef",
    "BuildRefs",
    "CreatorRef",
    "SnapshotRef",
    "VoiceRef",
    "WardrobeRef",
    "WorldRef",
]

# Creator DNA sections that shape behavior (CBS `dna_behavior_digest`, §15.6).
BEHAVIOR_SECTIONS = ("behavior", "gesture", "gaze", "camera", "avoidances", "personality")


@dataclass(frozen=True)
class AssetInfo:
    asset_id: UUID
    sha256: str
    storage_key: str
    mime: str
    bytes: int
    kind: str = "reference"


@dataclass(frozen=True)
class BrandKitRef:
    """A brand kit as build input (Phase 12): its content enters `render.final` by digest."""

    kit_id: UUID
    colors: Mapping[str, Any]
    fonts: Mapping[str, Any]
    caption_style_id: str | None
    logo: AssetInfo | None = None

    @property
    def digest(self) -> str:
        return content_digest(
            {
                "colors": dict(self.colors),
                "fonts": dict(self.fonts),
                "caption_style_id": self.caption_style_id,
                "logo": self.logo.sha256 if self.logo else None,
            }
        )


@dataclass(frozen=True)
class CreatorRef:
    version_id: UUID
    creator_id: UUID
    dna: Mapping[str, Any]
    appearance_version_id: UUID | None
    voice_version_id: UUID | None

    @classmethod
    def from_dna(
        cls,
        version_id: UUID,
        creator_id: UUID,
        dna: CreatorDNA,
        appearance_version_id: UUID | None,
        voice_version_id: UUID | None,
    ) -> CreatorRef:
        return cls(version_id, creator_id, dna.model_dump(mode="json"), appearance_version_id, voice_version_id)

    @property
    def display_name(self) -> str:
        return str(self.dna.get("identity", {}).get("display_name", ""))

    def behavior_digest(self) -> str:
        return content_digest({k: self.dna.get(k) for k in BEHAVIOR_SECTIONS})


@dataclass(frozen=True)
class AppearanceRef:
    version_id: UUID
    dna: Mapping[str, Any]
    canonical_face: AssetInfo | None

    def digest(self) -> str:
        return content_digest(
            {"dna": dict(self.dna), "face": self.canonical_face.sha256 if self.canonical_face else None}
        )


@dataclass(frozen=True)
class VoiceRef:
    version_id: UUID
    description: str
    references: tuple[Mapping[str, Any], ...]
    reference_assets: tuple[AssetInfo, ...]
    wpm: Mapping[str, float]
    lexicon: tuple[Mapping[str, Any], ...]
    default_prosody: Mapping[str, Any]

    @classmethod
    def from_dna(cls, version_id: UUID, dna: VoiceDNA, assets: Mapping[UUID, AssetInfo]) -> VoiceRef:
        data = dna.model_dump(mode="json")
        reference_assets = tuple(assets[r.asset_id] for r in dna.references if r.asset_id in assets)
        return cls(
            version_id=version_id,
            description=dna.description,
            references=tuple(data["references"]),
            reference_assets=reference_assets,
            wpm=dict(dna.wpm),
            lexicon=tuple(data["lexicon"]),
            default_prosody=data["default_prosody"],
        )

    def wpm_for(self, language: str, default: float) -> float:
        primary = language.split("-")[0]
        if language in self.wpm:
            return float(self.wpm[language])
        for tag, value in self.wpm.items():
            if tag.split("-")[0] == primary:
                return float(value)
        return default

    def conditioning_digest(self) -> str:
        """What `voice.prepare` reads: the references (by content) and the description."""
        return content_digest(
            {
                "description": self.description,
                "references": [
                    {"transcript": r.get("transcript"), "language": r.get("language")} for r in self.references
                ],
                "assets": sorted(a.sha256 for a in self.reference_assets),
            }
        )

    def synthesis_digest(self) -> str:
        """What `tts.segment` reads beyond the conditioning artifact: lexicon, WPM and prosody defaults."""
        return content_digest(
            {"wpm": dict(self.wpm), "lexicon": list(self.lexicon), "default_prosody": dict(self.default_prosody)}
        )


@dataclass(frozen=True)
class WardrobeRef:
    version_id: UUID
    spec: Mapping[str, Any]
    reference_assets: tuple[AssetInfo, ...] = ()

    def digest(self) -> str:
        return content_digest({"spec": dict(self.spec), "assets": sorted(a.sha256 for a in self.reference_assets)})


@dataclass(frozen=True)
class WorldRef:
    version_id: UUID
    dna: Mapping[str, Any]
    plates: Mapping[str, Mapping[str, Mapping[str, AssetInfo]]]  # camera position → time of day → weather

    @classmethod
    def from_dna(
        cls, version_id: UUID, dna: WorldDNA, plates: Mapping[str, Any], assets: Mapping[UUID, AssetInfo]
    ) -> WorldRef:
        """`plates` is `world_versions.plates` ({camera: {time: {weather: asset_id}}})."""
        resolved = {
            cam: {tod: {w: assets[UUID(str(a))] for w, a in ws.items()} for tod, ws in tods.items()}
            for cam, tods in plates.items()
        }
        return cls(version_id, dna.model_dump(mode="json"), resolved)

    def plate(self, camera_position: str, time_of_day: str, weather: str) -> AssetInfo | None:
        return self.plates.get(camera_position, {}).get(time_of_day, {}).get(weather)

    def nearest_plate(self, camera_position: str) -> AssetInfo | None:
        """A canonical plate of the same position (variations are conditioned on it, §19.2)."""
        by_time = self.plates.get(camera_position, {})
        default_time = self.dna.get("time_and_weather", {}).get("default_time_of_day")
        default_weather = self.dna.get("time_and_weather", {}).get("default_weather")
        if default_time in by_time and default_weather in by_time[default_time]:
            return by_time[default_time][default_weather]
        for weathers in by_time.values():
            for asset in weathers.values():
                return asset
        return None

    def visible_elements(self, camera_position: str) -> set[str] | None:
        """Elements a plate from `camera_position` shows: its background layout plus the elements
        continuity requires from it. None when the position has no layout (unknown: all)."""
        layouts = self.dna.get("background_layouts") or {}
        layout = layouts.get(camera_position)
        if layout is None:
            return None
        must = ((self.dna.get("continuity") or {}).get("must_show_from") or {}).get(camera_position, [])
        return set(layout.get("visible_elements", [])) | set(must)

    def plate_digest(self, camera_position: str, time_of_day: str, weather: str) -> str:
        """What a plate from one position can show (§19.5): the world's look, the elements visible
        from it (all when unknown) and the practical lights, lighting, time and weather rules, the
        position itself and the canonical plates it reuses or varies. Acoustics, zones and elements
        out of view never change a plate."""
        dna = self.dna
        visible = self.visible_elements(camera_position)
        practicals = {p.get("element") for p in (dna.get("lighting") or {}).get("practicals", [])}
        elements = [
            e
            for e in dna.get("elements", [])
            if visible is None or e.get("key") in visible or e.get("key") in practicals
        ]
        camera = next((c for c in dna.get("camera_positions", []) if c.get("key") == camera_position), None)
        canonical = self.plate(camera_position, time_of_day, weather)
        nearest = self.nearest_plate(camera_position)
        return content_digest(
            {
                "look": {k: dna.get(k) for k in ("name", "kind", "style_tags", "palette", "geometry")},
                "elements": elements,
                "lighting": dna.get("lighting"),
                "time_and_weather": dna.get("time_and_weather"),
                "camera": camera,
                "layout": (dna.get("background_layouts") or {}).get(camera_position),
                "must_show": ((dna.get("continuity") or {}).get("must_show_from") or {}).get(camera_position),
                "canonical": canonical.sha256 if canonical else None,
                "nearest": nearest.sha256 if nearest else None,
            }
        )

    def visual_digest(self) -> str:
        """Everything that can change a plate: the DNA minus acoustics, plus the canonical plates."""
        dna = {k: v for k, v in self.dna.items() if k != "acoustics"}
        plates = {
            cam: {tod: {w: a.sha256 for w, a in ws.items()} for tod, ws in tods.items()}
            for cam, tods in self.plates.items()
        }
        return content_digest({"dna": dna, "plates": plates})

    def behavior_digest(self) -> str:
        """World behavior digest (§15.6): elements, zones and camera positions; never lighting or acoustics."""
        elements = [
            {k: e.get(k) for k in ("key", "kind", "label", "states", "default_state", "mutability")}
            for e in self.dna.get("elements", [])
        ]
        return content_digest(
            {
                "elements": elements,
                "zones": self.dna.get("zones", []),
                "camera_positions": [c.get("key") for c in self.dna.get("camera_positions", [])],
            }
        )

    def acoustics_digest(self) -> str:
        return content_digest(self.dna.get("acoustics", {}))


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: UUID
    digest: str
    items: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_snapshot(cls, snapshot: MemorySnapshot) -> SnapshotRef:
        return cls(snapshot.id, snapshot.digest(), tuple(i.model_dump(mode="json") for i in snapshot.items))


@dataclass
class BuildRefs:
    creators: dict[UUID, CreatorRef] = field(default_factory=dict)
    appearances: dict[UUID, AppearanceRef] = field(default_factory=dict)
    voices: dict[UUID, VoiceRef] = field(default_factory=dict)
    wardrobes: dict[UUID, WardrobeRef] = field(default_factory=dict)
    worlds: dict[UUID, WorldRef] = field(default_factory=dict)
    snapshots: dict[UUID, SnapshotRef] = field(default_factory=dict)
    assets: dict[UUID, AssetInfo] = field(default_factory=dict)
    brand_kits: dict[UUID, BrandKitRef] = field(default_factory=dict)
    # `continuity_ref` of kind shot: "{version_id}:{shot_key}" → the sha256 of that shot's look
    # (its keyframe image); keys never enter cache keys, only the hash does (§12.2).
    shot_artifacts: dict[str, str] = field(default_factory=dict)

    def creator(self, version_id: UUID) -> CreatorRef:
        try:
            return self.creators[version_id]
        except KeyError:
            raise KeyError(f"creator version {version_id} is not resolved") from None

    def asset(self, asset_id: UUID) -> AssetInfo:
        try:
            return self.assets[asset_id]
        except KeyError:
            raise KeyError(f"asset {asset_id} is not resolved") from None
