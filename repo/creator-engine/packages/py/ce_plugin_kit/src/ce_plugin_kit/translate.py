"""Helpers for `BehaviorTranslator`s (§15.7, §37 translator conformance).

A translator turns `BehaviorDirectives` into engine syntax. Whatever the engine, every realization
must end up either *encoded* (the engine executes it: a prompt phrase, a parameter, a tag, an
inserted silence, the keyframe or the audio it inherits) or *reported* unsupported with a reason —
never dropped. `Translation` keeps that bookkeeping; `prompt_from_descriptions` assembles prompt text
from the model-agnostic vocabulary descriptions the directives carry (config/vocab/descriptions.yaml),
so prompt wording lives in the vocabulary and only the engine's syntax lives in the plugin (I1).
"""

from __future__ import annotations

import importlib.resources
from collections.abc import Iterable, Sequence
from functools import cache
from typing import Any

from ce_contracts.behavior import BehaviorDirectives, DirectiveRealization, DirectiveSubSpan
from ce_contracts.manifest import PluginManifest
from ce_contracts.plugins import load_manifest_text

__all__ = [
    "INHERITED_METHODS",
    "Translation",
    "default_reason",
    "global_prompt_translation",
    "intensity_phrase",
    "knob_mean",
    "locate",
    "map_knob",
    "own_manifest",
    "parse_label",
    "prompt_from_descriptions",
    "spans",
]

# Methods an audio-driven engine executes without any syntax: it inherits the keyframe it starts
# from, and the voice performance it lip-syncs to (§15.7 keyframe_conditioning, prosody_transfer).
INHERITED_METHODS = {"keyframe_conditioning": "keyframe", "prosody_transfer": "audio"}


@cache
def own_manifest(package: str) -> PluginManifest:
    """The manifest shipped next to a translator (knob ranges and prompt defaults live there, so the
    translator and the manifest never disagree)."""
    text = (importlib.resources.files(package) / "plugin.yaml").read_text(encoding="utf-8")
    return load_manifest_text(text, source=package)


def parse_label(value: str) -> tuple[str, float | None]:
    """`serious@0.6` → ("serious", 0.6); `look_away:down_left` → ("look_away:down_left", None)."""
    label, _, intensity = value.partition("@")
    try:
        return label, float(intensity) if intensity else None
    except ValueError:
        return label, None


def spans(compiled: BehaviorDirectives) -> list[DirectiveSubSpan]:
    return [span for visual in compiled.visual for span in visual.sub_spans]


def locate(compiled: BehaviorDirectives, item_ref: str) -> DirectiveSubSpan | None:
    for span in spans(compiled):
        if item_ref in span.item_refs:
            return span
    return None


def default_reason(realization: DirectiveRealization) -> str:
    """`unsupported` for omitted items, `realized_elsewhere:<method>` for items another node executes."""
    return "unsupported" if realization.method == "omit" else f"realized_elsewhere:{realization.method}"


def intensity_phrase(intensity: float | None) -> str:
    """A light qualifier for an intensity in [0, 1]: "slightly " below 0.35, "very " above 0.8."""
    if intensity is None:
        return ""
    if intensity < 0.35:
        return "slightly "
    if intensity > 0.8:
        return "very "
    return ""


def prompt_from_descriptions(fragments: Iterable[str], *, base: str = "", max_chars: int = 600) -> str:
    """Joins description fragments into one prompt: deduplicated, in order, sentence-cased, capped."""
    seen: list[str] = []
    for fragment in fragments:
        text = " ".join(str(fragment).split()).strip().rstrip(".")
        if text and text.lower() not in (s.lower() for s in seen):
            seen.append(text)
    body = "; ".join(seen)
    if body:
        body = f"{body[:1].upper()}{body[1:]}."
    head = base.strip().rstrip(".")
    out = f"{head}. {body}" if head and body else (f"{head}." if head else body)
    return out[:max_chars].rstrip()


class Translation:
    """Coverage bookkeeping for one translation: `encode()` or `report()` each realization once;
    `engine()` returns the `encoded`/`unsupported` lists merged into the engine payload and raises
    if any realization was left out (the contract suite checks the same from outside)."""

    def __init__(self, compiled: BehaviorDirectives) -> None:
        self.compiled = compiled
        self.encoded: list[dict[str, Any]] = []
        self.unsupported: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str, str]] = set()

    def _key(self, r: DirectiveRealization) -> tuple[str, str, str]:
        return (r.item_ref, r.dimension, r.method)

    def encode(self, realization: DirectiveRealization, **extra: Any) -> None:
        self._seen.add(self._key(realization))
        entry = {"item_ref": realization.item_ref, "dimension": realization.dimension, "method": realization.method}
        entry.update(extra)
        self.encoded.append(entry)

    def report(self, realization: DirectiveRealization, reason: str | None = None) -> None:
        self._seen.add(self._key(realization))
        self.unsupported.append(
            {
                "item_ref": realization.item_ref,
                "dimension": realization.dimension,
                "reason": reason or default_reason(realization),
            }
        )

    def pending(self) -> list[DirectiveRealization]:
        return [r for r in self.compiled.realizations if self._key(r) not in self._seen]

    def engine(self, payload: dict[str, Any]) -> dict[str, Any]:
        missing = self.pending()
        if missing:
            raise ValueError(f"translator left realizations unhandled: {[(r.item_ref, r.dimension) for r in missing]}")
        return {**payload, "encoded": self.encoded, "unsupported": self.unsupported}


def labels_in(span_list: Sequence[DirectiveSubSpan], dimension: str) -> list[str]:
    return [s.labels[dimension] for s in span_list if dimension in s.labels]


def knob_mean(compiled: BehaviorDirectives, knob: str) -> float | None:
    """The mean value of an abstract knob over the clip's sub-spans (knobs reach the directives only
    once calibrated, §15.7), or None when the compiler set none."""
    values = [s.knobs[knob] for s in spans(compiled) if knob in s.knobs]
    return sum(values) / len(values) if values else None


def map_knob(value: float, low: float, high: float) -> float:
    """An abstract knob value in [0, 1] → the engine parameter range of the manifest's knob."""
    return round(low + min(max(value, 0.0), 1.0) * (high - low), 4)


def global_prompt_translation(
    compiled: BehaviorDirectives, *, base: str, max_chars: int = 600
) -> tuple[Translation, str]:
    """The common translation of audio-driven, globally prompted avatar engines: `text_prompt_global`
    items become prompt phrases from their vocabulary descriptions; keyframe and prosody inheritance
    are executed without syntax; everything else is reported with its reason."""
    translation = Translation(compiled)
    fragments: list[str] = []
    for realization in compiled.realizations:
        if realization.method == "text_prompt_global":
            span = locate(compiled, realization.item_ref)
            if span is None:
                translation.report(realization, "outside_this_clip")
                continue
            label, intensity = parse_label(span.labels.get(realization.dimension, realization.dimension))
            text = span.descriptions.get(realization.dimension) or label.split(":")[0].replace("_", " ")
            fragments.append(intensity_phrase(intensity) + text)
            translation.encode(realization, control="text_global", label=label, text=text)
        elif realization.method in INHERITED_METHODS:
            translation.encode(realization, control=INHERITED_METHODS[realization.method])
        else:
            translation.report(realization)
    return translation, prompt_from_descriptions(fragments, base=base, max_chars=max_chars)
