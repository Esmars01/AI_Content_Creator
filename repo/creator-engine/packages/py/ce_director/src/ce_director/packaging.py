"""Director stage 12 — Packaging (§13, Phase 12): per-platform title, description, hashtags, CTA
and thumbnail texts, validated against limits.

Limits: a platform's own limit counts only when the platform file is verified (`verified_at` set,
D16) and the rule is not null; otherwise our **design default** (`packaging.design_limits`)
applies, labelled as such (`limit_sources`) — never presented as a platform fact.

The LLM writes the packaging from the planned spec (script, brief, hook) given as data (I10); the
answer is validated here (`problems`) and repaired by `ce_llm.structured`. When no LLM is available
(fixture miss, outage) or the answer stays invalid, the deterministic **template packaging** is
used and labelled (`generator.kind = template`, rule 5).

Pure functions and models; the job (`ce_exec.packaging_job`) does the I/O and the thumbnails.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "Limits",
    "PackagingDraft",
    "PlatformPackagingOut",
    "effective_limits",
    "normalize_hashtags",
    "problems",
    "spec_summary",
    "template_packaging",
]

_HASHTAG = re.compile(r"^[^\W_][\w]*$", re.UNICODE)
_WORD = re.compile(r"[^\W\d_][\w'-]{2,}", re.UNICODE)
_STOP = frozenset(
    [
        "the",
        "and",
        "for",
        "you",
        "your",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
        "were",
        "have",
        "has",
        "had",
        "not",
        "but",
        "what",
        "when",
        "why",
        "how",
        "who",
        "about",
        "into",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "here",
        "just",
        "like",
        "more",
        "most",
        "much",
        "many",
        "very",
        "will",
        "would",
        "could",
        "should",
        "can",
        "our",
        "out",
        "all",
        "any",
        "its",
        "it's",
        "i'm",
        "you're",
        "don't",
    ]
)


@dataclass(frozen=True)
class Limits:
    title_max_chars: int
    description_max_chars: int
    hashtags_max: int
    hashtag_max_chars: int
    cta_max_chars: int
    thumbnail_text_max_chars: int
    sources: dict[str, str] = field(default_factory=dict)  # limit name → "platform" | "design_default"

    def as_dict(self) -> dict[str, Any]:
        return {
            "title_max_chars": self.title_max_chars,
            "description_max_chars": self.description_max_chars,
            "hashtags_max": self.hashtags_max,
            "hashtag_max_chars": self.hashtag_max_chars,
            "cta_max_chars": self.cta_max_chars,
            "thumbnail_text_max_chars": self.thumbnail_text_max_chars,
            "sources": dict(self.sources),
        }


def effective_limits(platform: Any, design: Any, *, thumbnail_text_max_chars: int) -> Limits:
    """Platform rules where verified and set, our design defaults otherwise (labelled)."""
    verified = getattr(platform, "verified_at", None) is not None
    rules = getattr(platform, "rules", None)
    values: dict[str, int] = {}
    sources: dict[str, str] = {}
    for name in ("title_max_chars", "description_max_chars", "hashtags_max"):
        rule = getattr(rules, name, None) if verified else None
        values[name] = int(rule) if rule is not None else int(getattr(design, name))
        sources[name] = "platform" if rule is not None else "design_default"
    for name in ("hashtag_max_chars", "cta_max_chars"):  # no platform rule exists for these
        values[name] = int(getattr(design, name))
        sources[name] = "design_default"
    return Limits(**values, thumbnail_text_max_chars=thumbnail_text_max_chars, sources=sources)


def normalize_hashtags(tags: Sequence[str]) -> list[str]:
    """Without the prefix, NFC, de-duplicated case-insensitively, order kept."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = unicodedata.normalize("NFC", str(raw)).strip().lstrip("#＃").strip()
        if tag and tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
    return out


class PlatformPackagingOut(BaseModel):
    """What the model answers for one platform."""

    title: str = Field(min_length=1)
    description: str = ""
    hashtags: list[str] = Field(default_factory=list)
    cta_text: str = ""
    thumbnail_texts: list[str] = Field(default_factory=list, description="short overlay texts, best first")


def problems(out: PlatformPackagingOut, limits: Limits, *, thumbnails: int) -> list[str]:
    """Why a packaging answer is not acceptable (each is a repair instruction)."""
    found: list[str] = []
    title = out.title.strip()
    if not title:
        found.append("the title is empty")
    elif "\n" in title:
        found.append("the title must be one line")
    if len(title) > limits.title_max_chars:
        found.append(f"the title has {len(title)} characters; at most {limits.title_max_chars}")
    if len(out.description) > limits.description_max_chars:
        found.append(f"the description has {len(out.description)} characters; at most {limits.description_max_chars}")
    tags = normalize_hashtags(out.hashtags)
    if len(tags) > limits.hashtags_max:
        found.append(f"{len(tags)} hashtags; at most {limits.hashtags_max}")
    for tag in tags:
        if not _HASHTAG.match(tag):
            found.append(f"hashtag {tag!r} must be one word of letters, digits or underscores")
        elif len(tag) > limits.hashtag_max_chars:
            found.append(f"hashtag {tag!r} is longer than {limits.hashtag_max_chars} characters")
    if len(out.cta_text) > limits.cta_max_chars:
        found.append(f"the call to action has {len(out.cta_text)} characters; at most {limits.cta_max_chars}")
    if not out.thumbnail_texts:
        found.append(f"give {thumbnails} thumbnail texts")
    for text in out.thumbnail_texts:
        if not text.strip() or len(text) > limits.thumbnail_text_max_chars or "\n" in text.strip():
            found.append(
                f"thumbnail text {text!r} must be one non-empty line of at most "
                f"{limits.thumbnail_text_max_chars} characters"
            )
    return found


@dataclass
class PackagingDraft:
    platform: str
    title: str
    description: str
    hashtags: list[str]
    cta_text: str
    thumbnail_texts: list[str]
    limits: Limits
    issues: list[dict[str, Any]] = field(default_factory=list)
    generator: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_out(cls, platform: str, out: PlatformPackagingOut, limits: Limits, **kw: Any) -> PackagingDraft:
        return cls(
            platform=platform,
            title=out.title.strip(),
            description=out.description.strip(),
            hashtags=normalize_hashtags(out.hashtags),
            cta_text=out.cta_text.strip(),
            thumbnail_texts=[t.strip() for t in out.thumbnail_texts if t.strip()],
            limits=limits,
            **kw,
        )


def _clip(text: str, limit: int) -> str:
    """At most `limit` characters, cut at a word boundary with an ellipsis when cut."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[: max(1, limit - 1)].rsplit(" ", 1)[0].rstrip(",;:.-")
    return (cut or text[: limit - 1]) + "…"


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]


def spec_summary(spec: Mapping[str, Any]) -> dict[str, Any]:
    """The parts of a spec packaging is written from (the prompt's data and the template's input)."""
    brief = dict(spec.get("brief") or {})
    hooks = {h["key"]: h["text"] for h in brief.get("hook_candidates") or []}
    segments = [s["text"] for s in (spec.get("script") or {}).get("segments", [])]
    return {
        "title": str((spec.get("meta") or {}).get("title") or ""),
        "language": str((spec.get("meta") or {}).get("language") or ""),
        "angle": str(brief.get("angle") or ""),
        "audience": str(brief.get("audience") or ""),
        "hook": hooks.get(brief.get("selected_hook_key") or "", segments[0] if segments else ""),
        "script": " ".join(segments),
        "claims_unsupported": [
            c["text"] for c in (spec.get("research") or {}).get("claims", []) if c.get("verdict") != "supported"
        ],
    }


def _keywords(text: str, n: int) -> list[str]:
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for i, match in enumerate(_WORD.finditer(text)):
        word = match.group(0).strip("'-").lower()
        if word in _STOP or len(word) < 4:
            continue
        counts[word] = counts.get(word, 0) + 1
        first.setdefault(word, i)
    ranked = sorted(counts, key=lambda w: (-counts[w], first[w]))
    return [re.sub(r"[^\w]", "", w) for w in ranked[:n] if re.sub(r"[^\w]", "", w)]


def template_packaging(summary: Mapping[str, Any], platform: str, limits: Limits, *, thumbnails: int) -> PackagingDraft:
    """Deterministic packaging from the spec alone (labelled `template`): the title, the hook and
    the first sentences, keyword hashtags, no invented claims."""
    title = _clip(summary.get("title") or summary.get("hook") or "Untitled", limits.title_max_chars)
    sentences = _sentences(str(summary.get("script") or ""))
    description = _clip(" ".join(sentences[:2]) or str(summary.get("angle") or ""), limits.description_max_chars)
    tags = [t for t in _keywords(f"{summary.get('title', '')} {summary.get('script', '')}", limits.hashtags_max * 2)]
    tags = [t for t in tags if len(t) <= limits.hashtag_max_chars][: limits.hashtags_max]
    hook = str(summary.get("hook") or title)
    texts = [_clip(hook, limits.thumbnail_text_max_chars), _clip(title, limits.thumbnail_text_max_chars)]
    texts += [_clip(s, limits.thumbnail_text_max_chars) for s in sentences[1:]]
    unique: list[str] = []
    for text in texts:
        if text and text not in unique:
            unique.append(text)
    return PackagingDraft(
        platform=platform,
        title=title,
        description=description,
        hashtags=tags,
        cta_text="",
        thumbnail_texts=unique[: max(1, thumbnails)],
        limits=limits,
        generator={"kind": "template", "version": "packaging-template/v1"},
    )
