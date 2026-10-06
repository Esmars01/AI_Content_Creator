"""`captions_llm`: translates built captions with the configured LLM provider (§27, Phase 12).

1. Parse the ASS events (`Dialogue:` lines have ten fields; the text is the last and may contain
   commas and override tags). Consecutive events with the same plain text — the per-word
   highlight of one caption line — collapse into one line spanning them.
2. Translate the lines in batches (`lines_per_request`): each batch goes to the provider as a data
   block (I10) with `DATA_RULE`, and the answer must give back exactly the batch's indices with
   non-empty, single-line text within `max_line_chars`; `ce_llm.structured` repairs, then fails.
3. Write ASS (the source header and style, phrase-level events), SRT and VTT, each tagged with the
   language and `review_state: pending`.

The provider comes from the service settings at load (`llm_factory` in the load context); a test
can pass any `LLMProvider`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ce_contracts.common import LoadContext, RunContext
from ce_contracts.interfaces import CaptionEngine
from ce_contracts.models import CaptionResult, CaptionTranslateRequest, LLMMessage
from ce_llm import DATA_RULE, LLMProvider, structured, wrap_data
from pydantic import BaseModel, Field

__all__ = ["CaptionLine", "CaptionsLLM", "parse_ass", "render_ass", "render_srt", "render_vtt"]

_TAG = re.compile(r"\{[^}]*\}")
_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2})[.](\d{2})$")
STAGE = "captions_translate"


@dataclass(frozen=True)
class CaptionLine:
    start: str  # ASS time (h:mm:ss.cc)
    end: str
    style: str
    text: str  # plain text, override tags removed


def _plain(text: str) -> str:
    return _TAG.sub("", text).replace("\\N", " ").replace("\\n", " ").replace("\\h", " ").strip()


def parse_ass(document: str) -> tuple[list[str], list[CaptionLine]]:
    """(header lines up to and including the `[Events]` format line, caption lines)."""
    header: list[str] = []
    lines: list[CaptionLine] = []
    in_events = False
    for raw in document.splitlines():
        if not raw.startswith("Dialogue:"):
            if not lines:
                header.append(raw)
            if raw.strip() == "[Events]":
                in_events = True
            continue
        if not in_events:
            raise ValueError("a Dialogue line before the [Events] section")
        fields = raw[len("Dialogue:") :].lstrip().split(",", 9)
        if len(fields) != 10:
            raise ValueError(f"malformed Dialogue line: {raw[:80]!r}")
        start, end, style, text = fields[1], fields[2], fields[3], _plain(fields[9])
        if not text:
            continue
        previous = lines[-1] if lines else None
        if previous is not None and previous.text == text and previous.style == style:
            lines[-1] = CaptionLine(previous.start, end, style, text)  # the next highlight step of the line
        else:
            lines.append(CaptionLine(start, end, style, text))
    if not in_events:
        raise ValueError("no [Events] section")
    return header, lines


def _escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", " ")


def render_ass(header: list[str], lines: list[CaptionLine]) -> str:
    events = [f"Dialogue: 0,{ln.start},{ln.end},{ln.style},,0,0,0,,{_escape(ln.text)}" for ln in lines]
    return "\n".join([*header, *events]) + "\n"


def _seconds(ass_time: str) -> float:
    match = _TIME.match(ass_time.strip())
    if match is None:
        raise ValueError(f"invalid ASS time {ass_time!r}")
    h, m, s, cs = (int(g) for g in match.groups())
    return h * 3600 + m * 60 + s + cs / 100


def _stamp(seconds: float, sep: str) -> str:
    ms = round(seconds * 1000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def render_srt(lines: list[CaptionLine]) -> str:
    return "\n".join(
        f"{i}\n{_stamp(_seconds(ln.start), ',')} --> {_stamp(_seconds(ln.end), ',')}\n{ln.text}\n"
        for i, ln in enumerate(lines, start=1)
    )


def render_vtt(lines: list[CaptionLine]) -> str:
    blocks = [f"{_stamp(_seconds(ln.start), '.')} --> {_stamp(_seconds(ln.end), '.')}\n{ln.text}\n" for ln in lines]
    return "WEBVTT\n\n" + "\n".join(blocks)


class _Line(BaseModel):
    i: int = Field(ge=0)
    text: str


class _Batch(BaseModel):
    lines: list[_Line]


class CaptionsLLM(CaptionEngine):
    """The provider is created lazily from the load context (`llm_factory`), or set directly."""

    def __init__(self, manifest: Any, provider: LLMProvider | None = None) -> None:
        super().__init__(manifest)
        self.provider = provider
        self.options: dict[str, Any] = dict(manifest.defaults or {})
        self.limits: dict[str, Any] = {"lines_per_request": 40, "max_line_chars": 200}

    def use_backend(self, provider: LLMProvider) -> None:
        """The contract suite's stand-in provider (manifest `test_backend`)."""
        self.provider = provider

    async def load(self, ctx: LoadContext) -> None:
        await super().load(ctx)
        self.limits.update(dict(ctx.config.get("captions_translation") or {}))
        factory: Callable[[], LLMProvider] | None = ctx.config.get("llm_factory")
        if self.provider is None and factory is not None:
            self.provider = factory()

    def _messages(self, batch: list[tuple[int, str]], source: str, target: str) -> list[LLMMessage]:
        payload = json.dumps([{"i": i, "text": t} for i, t in batch], ensure_ascii=False)
        system = (
            "You translate short video captions. Translate each caption line from the source language to the "
            "target language, keeping its meaning, tone and length close to the original so it fits on screen. "
            'Keep names, brands and numbers. Answer with JSON: {"lines": [{"i": <index>, "text": '
            "<translation>}]} with exactly one entry per given index, one line each. " + DATA_RULE
        )
        user = f"Source language: {source}\nTarget language: {target}\nCaption lines:\n" + wrap_data(
            payload, kind="captions"
        )
        return [LLMMessage(role="system", content=system), LLMMessage(role="user", content=user)]

    async def _translate(self, batch: list[tuple[int, str]], source: str, target: str) -> dict[int, str]:
        assert self.provider is not None
        wanted = {i for i, _ in batch}
        limit = int(self.limits["max_line_chars"])

        def check(out: _Batch) -> list[str]:
            got = [ln.i for ln in out.lines]
            problems = []
            if sorted(got) != sorted(wanted):
                missing, extra = sorted(wanted - set(got)), sorted(set(got) - wanted)
                problems.append(f"give exactly one line per index; missing {missing}, unexpected {extra}")
            for ln in out.lines:
                if not ln.text.strip():
                    problems.append(f"line {ln.i} is empty")
                elif "\n" in ln.text.strip():
                    problems.append(f"line {ln.i} has a line break; give one line")
                elif len(ln.text) > limit:
                    problems.append(f"line {ln.i} is longer than {limit} characters")
            return problems

        result = await structured(
            self.provider,
            stage=STAGE,
            output_type=_Batch,
            messages=self._messages(batch, source, target),
            scenario_id=f"{STAGE}:{target}",
            check=check,
            max_repairs=int(self.options.get("max_repairs", 2)),
            temperature=float(self.options.get("temperature", 0.2)),
        )
        return {ln.i: ln.text.strip() for ln in result.value.lines}

    async def run_captions_translate(self, request: CaptionTranslateRequest, ctx: RunContext) -> CaptionResult:
        if self.provider is None:
            raise RuntimeError("captions_llm has no LLM provider (LLM_PROVIDER is not configured)")
        source = (await ctx.read_artifact(request.captions)).read_text(encoding="utf-8")
        header, lines = parse_ass(source)
        translated: dict[int, str] = {}
        size = int(self.limits["lines_per_request"])
        indexed = list(enumerate(ln.text for ln in lines))
        for start in range(0, len(indexed), size):
            translated.update(
                await self._translate(indexed[start : start + size], request.source_language, request.target_language)
            )
            await ctx.progress(min(1.0, (start + size) / max(1, len(indexed))))
        out_lines = [CaptionLine(ln.start, ln.end, ln.style, translated[i]) for i, ln in enumerate(lines)]
        language = request.target_language
        meta = {"language": language, "review_state": "pending", "highlight": "phrase", "lines": len(out_lines)}
        refs = {}
        workdir = ctx.scratch_dir / "captions_llm"
        workdir.mkdir(parents=True, exist_ok=True)
        for role, text, mime in (
            ("ass", render_ass(header, out_lines), "text/x-ssa"),
            ("srt", render_srt(out_lines), "application/x-subrip"),
            ("vtt", render_vtt(out_lines), "text/vtt"),
        ):
            path = workdir / f"captions.{language}.{role}"
            path.write_text(text, encoding="utf-8")
            refs[role] = await ctx.write_artifact(path, "captions", dict(meta), role=role, mime=mime)
        return CaptionResult(ass=refs["ass"], srt=refs["srt"], vtt=refs["vtt"])
