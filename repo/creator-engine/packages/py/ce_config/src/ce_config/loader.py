"""Loads and validates the whole `config/` tree (§35): schemas, cross-references, digests.

`load_config(root, app_env)` parses every file with its schema, checks references between
files (camera profile → LUT and mic profile, room → impulse response, mode → camera profiles,
world kinds and strategy packs, …) and vocabulary tokens, and computes a `config_digest` per
file for cache keys (§12.2). YAML digests hash the parsed content (canonical JSON), so comment
and formatting changes never invalidate caches; binary assets hash their bytes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from ce_core.canonical import content_digest
from ce_core.enums import Outcome
from ce_core.errors import Issue
from ce_core.vocab import Vocabulary, VocabularyError, load_vocabulary
from ce_core.yamlio import safe_load
from pydantic import BaseModel, ValidationError

from ce_config import schemas as s

__all__ = ["ConfigBundle", "ConfigError", "deep_merge", "load_config", "read_cube"]

M = TypeVar("M", bound=BaseModel)

APP_ENVS = ("dev", "test", "prod")


class ConfigError(ValueError):
    """The config tree cannot be loaded at all (missing default.yaml, broken vocabulary)."""


@dataclass
class LutInfo:
    path: Path
    size: int
    title: str


@dataclass
class ConfigBundle:
    root: Path
    app_env: str
    app: s.AppConfig
    vocab: Vocabulary
    languages: s.Languages | None = None
    memory: s.MemoryConfig | None = None
    intent_policies: s.IntentPolicies | None = None
    director: s.DirectorConfig | None = None
    routing: dict[str, s.RoutingProfile] = field(default_factory=dict)
    camera_profiles: dict[str, s.CameraProfile] = field(default_factory=dict)
    mic_profiles: dict[str, s.MicProfile] = field(default_factory=dict)
    rooms: dict[str, s.Room] = field(default_factory=dict)
    luts: dict[str, LutInfo] = field(default_factory=dict)
    modes: dict[str, s.Mode] = field(default_factory=dict)
    strategy_packs: dict[str, s.StrategyPack] = field(default_factory=dict)
    caption_styles: dict[str, s.CaptionStyle] = field(default_factory=dict)
    platforms: dict[str, s.Platform] = field(default_factory=dict)
    qc_tiers: dict[str, s.TierQC] = field(default_factory=dict)
    qc_behavior: s.BehaviorQC | None = None
    qc_world: s.WorldQC | None = None
    qc_consistency: s.ConsistencyQC | None = None
    operator_profile: s.OperatorProfile | None = None
    blocklists: s.Blocklists | None = None
    testimonials: s.TestimonialPolicy | None = None
    gpu_pools: s.GpuPools | None = None
    gpu_variants: s.GpuVariants | None = None
    models: list[s.ModelEntry] = field(default_factory=list)
    digests: dict[str, str] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    def render_preset(self, preset_id: str) -> s.RenderPreset | None:
        for platform in self.platforms.values():
            for preset in platform.render_presets:
                if preset.id == preset_id:
                    return preset
        return None

    def digest_of(self, *relative_paths: str) -> str:
        """Combined digest of several config files (what a node puts in its cache key)."""
        return content_digest({p: self.digests[p] for p in sorted(relative_paths)})


def deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def read_cube(path: Path) -> LutInfo:
    """Parses an Adobe .cube 3D LUT enough to validate it (size and value count, values finite)."""
    size = 0
    title = ""
    values = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("TITLE"):
            title = line[5:].strip().strip('"')
        elif line.startswith("LUT_3D_SIZE"):
            size = int(line.split()[1])
        elif line.startswith(("DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_SIZE")):
            if line.startswith("LUT_1D_SIZE"):
                raise ValueError("1D LUTs are not supported")
        else:
            parts = [float(x) for x in line.split()]
            if len(parts) != 3:
                raise ValueError(f"bad LUT row {line!r}")
            values += 1
    if size < 2 or values != size**3:
        raise ValueError(f"LUT_3D_SIZE {size} needs {size**3} rows, found {values}")
    return LutInfo(path=path, size=size, title=title)


class _Loader:
    def __init__(self, root: Path, app_env: str) -> None:
        self.root = root
        self.app_env = app_env
        self.issues: list[Issue] = []
        self.digests: dict[str, str] = {}

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def add(self, code: str, message: str, path: str | None = None, *, warning: bool = False) -> None:
        self.issues.append(Issue(code, message, path, {}, "warning" if warning else "error"))

    def read_yaml(self, path: Path) -> Any:
        try:
            data = safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.add("yaml", f"cannot parse: {exc}", self.rel(path))
            return None
        self.digests[self.rel(path)] = content_digest(data)
        return data

    def parse(self, model: type[M], path: Path) -> M | None:
        data = self.read_yaml(path)
        if data is None:
            return None
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            for err in exc.errors():
                loc = "/".join(str(x) for x in err["loc"])
                self.add("schema", f"{loc}: {err['msg']}", self.rel(path))
            return None

    def parse_dir(self, model: type[M], directory: str, key: Callable[[M], str]) -> dict[str, M]:
        out: dict[str, M] = {}
        for path in sorted((self.root / directory).glob("*.yaml")):
            parsed = self.parse(model, path)
            if parsed is None:
                continue
            ident = key(parsed)
            if ident != path.stem:
                self.add("id_mismatch", f"id {ident!r} must equal the file name {path.stem!r}", self.rel(path))
            out[ident] = parsed
        return out

    def hash_binary(self, path: Path) -> None:
        self.digests[self.rel(path)] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_config(root: Path | str, app_env: str = "dev") -> ConfigBundle:
    root = Path(root)
    if app_env not in APP_ENVS:
        raise ConfigError(f"APP_ENV must be one of {APP_ENVS}, got {app_env!r}")
    if not (root / "default.yaml").is_file():
        raise ConfigError(f"{root / 'default.yaml'} not found")
    try:
        vocab = load_vocabulary(root / "vocab")
    except VocabularyError as exc:
        raise ConfigError(str(exc)) from exc
    loader = _Loader(root, app_env)
    for name in sorted((root / "vocab").glob("*.yaml")):
        loader.read_yaml(name)
    loader.issues += [Issue(i.code, i.message, "vocab", i.detail, i.severity) for i in vocab.lint()]

    base = loader.read_yaml(root / "default.yaml") or {}
    overlays: dict[str, Any] = {}
    for env in APP_ENVS:
        path = root / "env" / f"{env}.yaml"
        if path.is_file() and loader.parse(s.EnvOverlay, path) is not None:
            overlays[env] = loader.read_yaml(path) or {}
    try:
        app = s.AppConfig.model_validate(deep_merge(base, overlays.get(app_env, {})))
    except ValidationError as exc:
        raise ConfigError(f"default.yaml + env/{app_env}.yaml: {exc}") from exc

    bundle = ConfigBundle(root=root, app_env=app_env, app=app, vocab=vocab)
    bundle.languages = loader.parse(s.Languages, root / "languages.yaml")
    bundle.memory = loader.parse(s.MemoryConfig, root / "memory.yaml")
    bundle.intent_policies = loader.parse(s.IntentPolicies, root / "intent_policies.yaml")
    bundle.director = loader.parse(s.DirectorConfig, root / "director.yaml")
    bundle.routing = loader.parse_dir(s.RoutingProfile, "routing", lambda m: m.id)
    bundle.camera_profiles = loader.parse_dir(s.CameraProfile, "camera_profiles", lambda m: m.id)
    bundle.mic_profiles = loader.parse_dir(s.MicProfile, "mic_profiles", lambda m: m.id)
    bundle.rooms = loader.parse_dir(s.Room, "rooms", lambda m: m.id)
    bundle.modes = loader.parse_dir(s.Mode, "modes", lambda m: m.id)
    bundle.strategy_packs = loader.parse_dir(s.StrategyPack, "strategy_packs", lambda m: m.id)
    bundle.caption_styles = loader.parse_dir(s.CaptionStyle, "caption_styles", lambda m: m.id)
    bundle.platforms = loader.parse_dir(s.Platform, "platforms", lambda m: m.id)
    for tier in ("draft", "final"):
        parsed = loader.parse(s.TierQC, root / "qc" / f"{tier}.yaml")
        if parsed is not None:
            if parsed.tier != tier:
                loader.add("id_mismatch", f"qc/{tier}.yaml declares tier {parsed.tier}", f"qc/{tier}.yaml")
            bundle.qc_tiers[tier] = parsed
    bundle.qc_behavior = loader.parse(s.BehaviorQC, root / "qc" / "behavior.yaml")
    bundle.qc_world = loader.parse(s.WorldQC, root / "qc" / "world.yaml")
    bundle.qc_consistency = loader.parse(s.ConsistencyQC, root / "qc" / "consistency.yaml")
    bundle.operator_profile = loader.parse(s.OperatorProfile, root / "policy" / "operator_profile.yaml")
    bundle.blocklists = loader.parse(s.Blocklists, root / "policy" / "blocklists.yaml")
    bundle.testimonials = loader.parse(s.TestimonialPolicy, root / "policy" / "testimonials.yaml")
    bundle.gpu_pools = loader.parse(s.GpuPools, root / "gpu" / "pools.yaml")
    if (root / "gpu" / "variants.yaml").is_file():
        bundle.gpu_variants = loader.parse(s.GpuVariants, root / "gpu" / "variants.yaml")
    for path in sorted((root / "models").glob("*.yaml")):
        registry = loader.parse(s.ModelRegistry, path)
        if registry is not None:
            bundle.models.extend(registry.models)
    for path in sorted((root / "luts").glob("*.cube")):
        loader.hash_binary(path)
        try:
            bundle.luts[path.stem] = read_cube(path)
        except ValueError as exc:
            loader.add("lut", str(exc), loader.rel(path))
    for path in sorted((root / "rooms" / "impulse_responses").glob("*.wav")):
        loader.hash_binary(path)

    _cross_check(bundle, loader)
    bundle.digests = loader.digests
    bundle.issues = loader.issues
    return bundle


def _cross_check(b: ConfigBundle, loader: _Loader) -> None:
    v = b.vocab
    add = loader.add

    def vocab(category: str, token: str, where: str) -> None:
        if not v.has(category, token):
            add("unknown_vocab", f"{token!r} is not a {category}", where)

    for cam in b.camera_profiles.values():
        where = f"camera_profiles/{cam.id}.yaml"
        lut = Path(cam.color.lut)
        if lut.parent.as_posix() != "luts" or lut.stem not in b.luts:
            add("missing_ref", f"LUT {cam.color.lut} does not exist", where)
        if cam.audio.mic_profile not in b.mic_profiles:
            add("missing_ref", f"mic profile {cam.audio.mic_profile} does not exist", where)
        vocab("camera.framing", cam.framing.default, where)
    for room in b.rooms.values():
        where = f"rooms/{room.id}.yaml"
        if not (b.root / "rooms" / room.impulse_response).is_file():
            add("missing_ref", f"impulse response {room.impulse_response} does not exist", where)
        vocab("ambient_profile", room.default_ambient, where)
    for mode in b.modes.values():
        where = f"modes/{mode.id}.yaml"
        for cam_id in mode.default_camera_profiles:
            if cam_id not in b.camera_profiles:
                add("missing_ref", f"camera profile {cam_id} does not exist", where)
        for kind in mode.default_world_kinds:
            vocab("world_kind", kind, where)
        for pack in mode.strategy_packs:
            if pack not in b.strategy_packs:
                add("missing_ref", f"strategy pack {pack} does not exist", where)
        d = mode.duration_s
        if not d.min <= d.default <= d.max:
            add("range", "duration_s must satisfy min <= default <= max", where)
        if mode.maturity == "production" and mode.roadmap != "MVP":
            add("maturity", "production modes must be MVP", where)
    for strategy_pack in b.strategy_packs.values():
        for purpose in strategy_pack.structure:
            vocab("intent.scene_purpose", purpose, f"strategy_packs/{strategy_pack.id}.yaml")
    preset_ids: dict[str, str] = {}
    for platform in b.platforms.values():
        where = f"platforms/{platform.id}.yaml"
        for preset in platform.render_presets:
            if preset.id in preset_ids:
                add("duplicate", f"render preset {preset.id} is also defined in {preset_ids[preset.id]}", where)
            preset_ids[preset.id] = where
            w, h = (int(x) for x in preset.aspect.split(":"))
            if abs(preset.width * h - preset.height * w) > max(w, h):
                add("aspect", f"preset {preset.id} is {preset.width}x{preset.height}, not {preset.aspect}", where)
        if platform.verified_at is None and any(
            getattr(platform.rules, f) is not None
            for f in ("max_duration_s", "title_max_chars", "description_max_chars", "hashtags_max")
        ):
            add("unverified", "platform limits are set but verified_at is null (rule 14)", where)
    if b.intent_policies is not None:
        groups = set(b.intent_policies.precedence)
        ids: set[str] = set()
        for rule in b.intent_policies.rules:
            where = f"intent_policies.yaml#{rule.id}"
            if rule.id in ids:
                add("duplicate", f"rule id {rule.id} is used twice", where)
            ids.add(rule.id)
            if rule.group not in groups:
                add("missing_ref", f"group {rule.group} is not in precedence", where)
            for field_name, token in rule.match.items():
                if field_name == "mode":
                    if b.modes and token not in b.modes:
                        add("missing_ref", f"mode {token} does not exist", where)
                elif field_name == "position":
                    if token not in s.POLICY_POSITIONS:
                        add("schema", f"position {token} is not one of {s.POLICY_POSITIONS}", where)
                elif field_name not in s.INTENT_FIELDS:
                    add("schema", f"match field {field_name} is not an intent field, mode or position", where)
                else:
                    vocab(f"intent.{field_name}", token, where)
    if b.memory is not None:
        categories = v.tokens("memory_category")
        for section in (b.memory.retrieval.budgets, b.memory.retrieval.recency_half_life_days):
            unknown = sorted(set(section) - categories)
            missing = sorted(categories - set(section))
            if unknown:
                add("unknown_vocab", f"unknown memory categories {unknown}", "memory.yaml")
            if missing:
                add("missing_ref", f"memory categories without settings {missing}", "memory.yaml")
        for habit in b.memory.observation_habits:
            habit_kind = v.memory_kinds.get(habit.kind)
            if habit_kind is None:
                add("unknown_vocab", f"observation habit: unknown memory kind {habit.kind!r}", "memory.yaml")
            elif habit_kind.fields.get(habit.field) != "unit" or habit_kind.key_fields:
                add(
                    "schema",
                    f"observation habit {habit.kind}.{habit.field}: needs a unit field of a kind without key fields",
                    "memory.yaml",
                )
    if b.director is not None:
        dc = b.director
        where = "director.yaml"
        if b.modes and dc.template.mode not in b.modes:
            add("missing_ref", f"template mode {dc.template.mode} does not exist", where)
        if dc.hooks.min > dc.hooks.max:
            add("schema", "hooks.min exceeds hooks.max", where)
        for goal in dc.cta_texts:
            vocab("intent.cta_goal", goal, where)
        vocab("situation_kind", dc.template.situation_kind, where)
        vocab("audience_stance", dc.template.audience_stance, where)
        for name, token in dc.template.video_intent.items():
            if name == "emotional_arc" or f"intent.{name}" not in v.categories:
                add("schema", f"template video_intent field {name} is not an intent field", where)
            else:
                vocab(f"intent.{name}", token, where)
        purposes = v.tokens("intent.scene_purpose")
        missing = sorted(purposes - set(dc.template.purposes))
        if missing:
            add("missing_ref", f"template purposes without defaults {missing}", where)
        template_fields = {
            "narrative_goal": "intent.narrative_goal",
            "emotional_goal": "intent.emotional_goal",
            "audience_effect": "intent.audience_effect",
            "information_goal": "intent.information_goal",
            "attention_goal": "intent.attention_goal",
            "emotion": "emotion",
            "internal_state": "internal_state",
            "social_goal": "social_goal",
            "audience_goal": "audience_goal",
            "performance_intent": "performance_intent",
            "prosody": "strategy.prosody",
            "gaze": "strategy.gaze",
            "gesture": "strategy.gesture",
            "reaction": "strategy.reaction",
            "camera_awareness": "strategy.camera_awareness",
        }
        for purpose, defaults in dc.template.purposes.items():
            vocab("intent.scene_purpose", purpose, f"{where}#{purpose}")
            for field_name, category in template_fields.items():
                vocab(category, getattr(defaults, field_name), f"{where}#{purpose}.{field_name}")
    if b.testimonials is not None:
        where = "policy/testimonials.yaml"
        for mode_id in (*b.testimonials.applies_to_modes, *b.testimonials.dramatization_modes):
            if b.modes and mode_id not in b.modes:
                add("missing_ref", f"mode {mode_id} does not exist", where)
        for rule_id, pattern in b.testimonials.experiential_patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                add("schema", f"pattern {rule_id} is not a valid regex: {exc}", where)
    if b.languages is not None:
        en = b.languages.languages.get("en")
        if en is None or en.support != "production":
            add("languages", "English must be a production language (§39.4)", "languages.yaml")
    if b.qc_behavior is not None:
        outcomes = set(Outcome)
        for name in (
            *b.qc_behavior.must.retry_on,
            *b.qc_behavior.must.retry_on_unexpected_editorial,
            *b.qc_behavior.must.warn_on,
            *b.qc_behavior.never_fail,
        ):
            if name not in outcomes:
                add("unknown_outcome", f"{name} is not one of the 12 outcomes", "qc/behavior.yaml")
    if b.qc_world is not None:
        for kind in b.qc_world.thresholds_by_world_kind:
            if kind != "default":
                vocab("world_kind", kind, "qc/world.yaml")
    if b.gpu_pools is not None:
        seen: set[str] = set()
        for pool in b.gpu_pools.pools:
            if pool.id in seen:
                add("duplicate", f"pool id {pool.id} is used twice", "gpu/pools.yaml")
            seen.add(pool.id)
            if pool.min > pool.max:
                add("range", f"pool {pool.id}: min > max", "gpu/pools.yaml")
            unknown = sorted(set(pool.gpu_classes) - set(b.gpu_pools.classes))
            if unknown:
                add(
                    "unknown_gpu_class", f"pool {pool.id}: GPU classes without a VRAM entry {unknown}", "gpu/pools.yaml"
                )
    for model in b.models:
        if not model.license.commercial_use and model.status == "production":
            add("license", f"model {model.key} is non-commercial but marked production (rule 15)", "models")
        for dep in model.dependencies:
            if not dep.license.commercial_use and model.status == "production":
                add("license", f"model {model.key} depends on non-commercial {dep.ref} (ADR 0012)", "models")
    behavior = b.app.behavior
    for strategy in behavior.prosody_strategies:
        vocab("strategy.prosody", strategy, "default.yaml#behavior.prosody_strategies")
    missing_prosody = sorted(set(v.categories.get("strategy.prosody", ())) - set(behavior.prosody_strategies))
    if missing_prosody:
        add("missing_ref", f"prosody strategies without defaults {missing_prosody}", "default.yaml#behavior")
    for channel, tokens in behavior.low_salience.items():
        for token in tokens:
            vocab(f"strategy.{channel}", token, "default.yaml#behavior.low_salience")
    for kind in behavior.attention_element_kinds:
        vocab("element_kind", kind, "default.yaml#behavior.attention_element_kinds")
    if b.app_env == "prod" and b.app.provenance.mode != "real":
        add("provenance", "env/prod.yaml must resolve provenance.mode to real (I11)", "env/prod.yaml")
    ph = b.app.security.password_hash
    if b.app_env == "prod" and (ph.memory_cost_kib < 19_456 or ph.time_cost < 2):
        add("password_hash", "production argon2id needs at least 19 MiB and time_cost 2 (OWASP)", "env/prod.yaml")
