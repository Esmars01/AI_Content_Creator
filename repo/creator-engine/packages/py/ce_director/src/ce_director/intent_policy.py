"""The intent policy engine (§14, `config/intent_policies.yaml`).

A deterministic rule engine. For each scene it collects the facts — the scene intent, the video
`cta_goal` on the last scene, the mode and the scene's position — and every rule whose `match`
holds proposes defaults per channel. For one (channel, key) the rule of the earliest precedence
group wins (rules of one group in file order). The result is a `PolicySet`:

- `editing` proposals shape the shot plan (cuts are kept away from a reveal; a pattern interrupt
  gets an early cut);
- `acting` and voice `prosody_sequence` proposals are handed to the acting stage, which may depart
  from them only with a recorded reason;
- `apply()` renders the remaining channels into the spec with `derived_from: intent` (or
  `source: intent_policy` on annotations): pauses and emphasis around the reveal, punch-ins,
  overlay exclusion at the reveal, music cues that drop before and resolve after a reveal, and
  the CTA end card. `optional` proposals and keys without a spec field are recorded as decisions
  that were not applied, with the reason, so the plan report shows every proposal.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ce_config.schemas import INTENT_FIELDS, DirectorConfig, IntentPolicies, Mode
from ce_core.keys import KeyKind

from ce_director.draft import SpecDraft, word_span

__all__ = ["IntentPolicyEngine", "PolicyDecision", "PolicySet", "Proposal"]

VIDEO_ONLY = frozenset({"cta_goal"})


@dataclass(frozen=True)
class Proposal:
    rule_id: str
    group: str
    channel: str
    key: str
    value: Any
    intent_ref: str  # SpecPath of the intent field the rule matched

    def derived(self) -> dict[str, str]:
        return {"kind": "intent", "ref": self.intent_ref}


@dataclass
class PolicyDecision:
    rule_id: str
    scene_key: str
    channel: str
    key: str
    value: Any
    intent_ref: str
    applied: bool
    effect: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "scene_key": self.scene_key,
            "channel": self.channel,
            "key": self.key,
            "value": self.value,
            "intent_ref": self.intent_ref,
            "applied": self.applied,
            "effect": self.effect,
        }


@dataclass
class PolicySet:
    by_scene: dict[str, dict[tuple[str, str], Proposal]] = field(default_factory=dict)

    def get(self, scene_key: str, channel: str, key: str) -> Proposal | None:
        return self.by_scene.get(scene_key, {}).get((channel, key))

    def channel(self, scene_key: str, channel: str) -> dict[str, Proposal]:
        return {k: p for (c, k), p in self.by_scene.get(scene_key, {}).items() if c == channel}

    def proposals(self) -> list[tuple[str, Proposal]]:
        return [
            (scene, p)
            for scene, items in sorted(self.by_scene.items())
            for _, p in sorted(items.items(), key=lambda kv: kv[0])
        ]


class IntentPolicyEngine:
    def __init__(self, policies: IntentPolicies) -> None:
        self.policies = policies
        self._rank = {group: i for i, group in enumerate(policies.precedence)}

    def facts(self, draft: SpecDraft, scene: Mapping[str, Any], mode: str) -> dict[str, tuple[str, str]]:
        """Fact → (value, SpecPath of where it came from)."""
        scenes = draft.scenes()
        index = next(i for i, s in enumerate(scenes) if s["key"] == scene["key"])
        position = (
            "only" if len(scenes) == 1 else "first" if index == 0 else "last" if index == len(scenes) - 1 else "middle"
        )
        out: dict[str, tuple[str, str]] = {
            "mode": (mode, "/meta/mode"),
            "position": (position, f"/scenes[{scene['key']}]/order"),
        }
        for name, value in (scene.get("intent") or {}).items():
            if name in INTENT_FIELDS and isinstance(value, str):
                out[name] = (value, f"/scenes[{scene['key']}]/intent/{name}")
        if position in ("last", "only"):
            video = draft.data.get("intent", {}).get("video", {})
            for name in VIDEO_ONLY:
                if isinstance(video.get(name), str):
                    out[name] = (video[name], f"/intent/video/{name}")
        return out

    def evaluate(self, draft: SpecDraft, mode: str) -> PolicySet:
        result = PolicySet()
        ordered = sorted(
            enumerate(self.policies.rules), key=lambda ir: (self._rank.get(ir[1].group, len(self._rank)), ir[0])
        )
        for scene in draft.scenes():
            facts = self.facts(draft, scene, mode)
            chosen: dict[tuple[str, str], Proposal] = {}
            for _, rule in ordered:
                if not all(facts.get(k, (None, ""))[0] == v for k, v in rule.match.items()):
                    continue
                intent_key = next((k for k in rule.match if k in INTENT_FIELDS), next(iter(rule.match)))
                ref = facts[intent_key][1]
                for channel, values in rule.proposals.items():
                    for key, value in values.items():
                        chosen.setdefault((channel, key), Proposal(rule.id, rule.group, channel, key, value, ref))
            result.by_scene[scene["key"]] = chosen
        return result

    # ------------------------------------------------------------------ rendering
    def apply(
        self,
        draft: SpecDraft,
        policies: PolicySet,
        *,
        reveal: Mapping[str, tuple[str, int]],
        mode: Mode,
        config: DirectorConfig,
        pause_ms: Mapping[str, int],
        wpm: float,
    ) -> list[PolicyDecision]:
        decisions: list[PolicyDecision] = []
        for scene_key, proposal in policies.proposals():
            applied, effect = self._render(
                draft, scene_key, proposal, reveal.get(scene_key), mode, config, pause_ms, wpm
            )
            decisions.append(
                PolicyDecision(
                    proposal.rule_id,
                    scene_key,
                    proposal.channel,
                    proposal.key,
                    proposal.value,
                    proposal.intent_ref,
                    applied,
                    effect,
                )
            )
        return decisions

    def _render(
        self,
        draft: SpecDraft,
        scene_key: str,
        p: Proposal,
        reveal: tuple[str, int] | None,
        mode: Mode,
        config: DirectorConfig,
        pause_ms: Mapping[str, int],
        wpm: float,
    ) -> tuple[bool, str]:
        scene = draft.scene(scene_key)
        words = draft.scene_words(scene_key)
        if not words:
            return False, "the scene has no speech"
        channel, key, value = p.channel, p.key, p.value
        if value == "optional":
            return False, "optional proposal; not applied by default"
        if value is False and (channel, key) != ("broll", "overlay_during_reveal"):  # false = exclude overlays
            return False, "proposal is off"
        if channel in ("acting",) or (channel == "voice" and key == "prosody_sequence"):
            return True, "handed to the acting stage (see the acting stage's deviations)"
        if channel == "editing" and key in ("hold_through_reveal", "no_cut_within_words", "first_cut_within_s"):
            return True, "applied by the shot plan"
        if channel == "broll" and key == "overlay_during_tease":
            return True, "overlays stay allowed before the reveal"
        needs_reveal = {
            ("voice", "pause_before_reveal_ms"),
            ("voice", "emphasis_on_reveal_word"),
            ("voice", "emphasis_on_answer"),
            ("camera", "punch_in_on_reveal_word"),
            ("broll", "overlay_during_reveal"),
            ("captions", "emphasis_on_reveal"),
            ("captions", "hide_reveal_keyword_until_spoken"),
            ("music", "drop_beats_before_reveal"),
            ("music", "resolve_after_reveal"),
        }
        if (channel, key) in needs_reveal and reveal is None:
            return False, "the scene names no reveal word"
        if channel == "voice" and key == "pause_before_reveal_ms" and reveal is not None:
            position = words.index(reveal)
            if position == 0:
                return False, "the reveal is the scene's first word; no word to pause after"
            lo, hi = (value if isinstance(value, list) else [value, value])[:2]
            tag = min(pause_ms, key=lambda t: (0 if lo <= pause_ms[t] <= hi else 1, abs(pause_ms[t] - (lo + hi) / 2)))
            before = words[position - 1]
            made = draft.add_annotation(before[0], "pause", tag, before[1], source="intent_policy")
            return True, f"{tag} after {before[0]}.w{before[1]}" + ("" if made else " (already present)")
        if channel in ("voice", "captions") and key in (
            "emphasis_on_reveal_word",
            "emphasis_on_answer",
            "emphasis_on_reveal",
        ):
            assert reveal is not None
            made = draft.add_annotation(reveal[0], "emphasis", "emphasize", reveal[1], source="intent_policy")
            return True, f"emphasis on {reveal[0]}.w{reveal[1]}" + ("" if made else " (already present)")
        if channel == "captions" and key == "hide_reveal_keyword_until_spoken":
            return True, "captions reveal each word as it is spoken (active-word captions, §27)"
        if channel == "camera" and key in ("punch_in_on_reveal_word", "punch_in_on_first_word"):
            at = reveal if key == "punch_in_on_reveal_word" else words[0]
            assert at is not None
            if "editorial_punch" not in mode.editorial_methods:
                return False, f"mode {mode.id} does not allow punch-ins"
            move = draft.add_camera_move(
                scene_key, at, "punch_in", scale=config.punch_scale, derived_from=[p.derived()]
            )
            return (move is not None), (f"punch-in {move} at {at[0]}.w{at[1]}" if move else "no talking shot there")
        if channel == "broll" and key == "overlay_during_reveal" and reveal is not None:
            if value is not False:
                return True, "overlays are allowed at the reveal"
            position = words.index(reveal)
            kept, dropped = [], []
            for shot in scene["shots"]:
                rng = draft.span_range(scene_key, shot["span"])
                covers = rng is not None and rng[0] <= position <= rng[1]
                if shot["layer"] == "overlay" and shot["type"] != "title_card" and covers:
                    dropped.append(shot["key"])
                else:
                    kept.append(shot)
            scene["shots"] = kept
            return (
                True,
                f"removed overlays covering the reveal: {dropped}" if dropped else "no overlay covers the reveal",
            )
        if channel == "music" and key in ("drop_beats_before_reveal", "resolve_after_reveal") and reveal is not None:
            return self._music_reveal(draft, scene_key, p, reveal, config)
        if channel == "music" and key == "sustain_tension":
            cue = self._scene_cue(draft, scene_key)
            if cue is None:
                return False, "the scene has no music cue"
            cue["mood"] = config.music.tension_mood
            cue.setdefault("derived_from", []).append(p.derived())
            return True, f"music cue {cue['key']} sustains tension"
        if channel == "captions" and key == "cta_overlay":
            return True, "rendered as the end card (editing.end_card_s)"
        if channel == "editing" and key == "end_card_s":
            return self._end_card(draft, scene_key, p, mode, config, wpm)
        return False, f"no renderer for {channel}.{key} in this phase"

    @staticmethod
    def _scene_cue(draft: SpecDraft, scene_key: str) -> dict[str, Any] | None:
        for cue in draft.data["audio"]["music"]["cues"]:
            span = cue["span"]
            if span.get("kind") == "scene" and span.get("scene_key") == scene_key:
                return cue
            owner = draft.scene_of_segment(span["start"]["segment_key"]) if span.get("kind") == "words" else None
            if owner is not None and owner["key"] == scene_key:
                return cue
        return None

    def _music_reveal(
        self, draft: SpecDraft, scene_key: str, p: Proposal, reveal: tuple[str, int], config: DirectorConfig
    ) -> tuple[bool, str]:
        """Splits the scene's cue: one cue up to the word before the reveal, one from the reveal."""
        words = draft.scene_words(scene_key)
        position = words.index(reveal)
        cues = draft.data["audio"]["music"]["cues"]
        existing = [c for c in cues if c.get("span", {}).get("kind") == "scene" and c["span"]["scene_key"] == scene_key]
        split = [c for c in cues if any(d.get("ref") == p.intent_ref for d in c.get("derived_from", []))]
        if split:
            for cue in split:
                if p.derived() not in cue["derived_from"]:
                    cue["derived_from"].append(p.derived())
            return True, "music already drops before and resolves after the reveal"
        if not existing or position == 0:
            return False, "no scene-long music cue to shape" if not existing else "the reveal is the first word"
        base = existing[0]
        cues.remove(base)
        before = {
            **base,
            "span": word_span(words[0], words[position - 1]),
            "mood": config.music.tension_mood,
            "derived_from": [p.derived()],
        }
        after = {
            **base,
            "key": "",
            "span": word_span(words[position], words[-1]),
            "mood": config.music.resolve_mood,
            "derived_from": [p.derived()],
        }
        cues.append(before)
        after["key"] = draft.new_key(KeyKind.MUSIC_CUE)
        cues.append(after)
        return True, f"music {before['key']} drops before the reveal; {after['key']} resolves from it"

    def _end_card(
        self, draft: SpecDraft, scene_key: str, p: Proposal, mode: Mode, config: DirectorConfig, wpm: float
    ) -> tuple[bool, str]:
        cta = draft.data.get("intent", {}).get("video", {}).get("cta_goal")
        text = config.cta_texts.get(str(cta)) if cta else None
        scene = draft.scene(scene_key)
        if not text:
            return False, f"no end-card text for cta_goal {cta!r}"
        if "title_card" not in [str(t) for t in mode.allowed_shot_types]:
            return False, f"mode {mode.id} does not allow title cards"
        words = draft.scene_words(scene_key)
        count = max(1, min(len(words), round(float(p.value) * wpm / 60.0)))
        span = word_span(words[-count], words[-1])
        for shot in scene["shots"]:
            if shot["type"] == "title_card" and shot.get("title", {}).get("text") == text:
                return True, f"end card {shot['key']} already present"
        key = draft.new_key(KeyKind.SHOT)
        profile = next((s["camera"]["profile_id"] for s in scene["shots"] if s["type"] == "talking_head"), None)
        scene["shots"].append(
            {
                "key": key,
                "type": "title_card",
                "layer": "overlay",
                "span": span,
                "character_key": None,
                "camera": {
                    "profile_id": profile or "phone_front_selfie",
                    "framing": "medium",
                    "angle": "eye_level",
                    "moves": [],
                },
                "title": {"text": text, "style_id": None},
                "takes": {"count": 1, "selected_take_key": None},
                "derived_from": [p.derived()],
            }
        )
        return True, f"end card {key} ({text!r}) over the last {count} words"
