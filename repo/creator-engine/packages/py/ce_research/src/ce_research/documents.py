"""Sources and chunks for in-memory research (Phase 4; persistent ingestion is Phase 12).

A source is pasted text or a fetched page. Its text is data, never instructions (I10): it reaches
prompts only through `ce_llm.wrap_data`. Source ids are UUIDv5 of the content digest, so the same
text always gets the same id and evidence ids (`<source_id>#c<index>`) are stable across runs.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

import regex

__all__ = ["Chunk", "Source", "chunk_source", "html_to_text", "make_source"]

_NAMESPACE = uuid.UUID("7d0f3c1e-5b8a-4f7e-9a51-3c2b6d8e4f10")
_SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe", "object"})
_BLOCK = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
     "header", "footer", "blockquote", "pre", "table", "hr", "dd", "dt", "figcaption"}
)  # fmt: skip
_WORD = regex.compile(r"\S+")


@dataclass(frozen=True)
class Source:
    id: uuid.UUID
    kind: Literal["pasted", "url"]
    text: str
    title: str = ""
    url: str | None = None
    digest: str = ""


@dataclass(frozen=True)
class Chunk:
    source_id: uuid.UUID
    index: int
    text: str
    start: int  # character offsets into the source text
    end: int

    @property
    def id(self) -> str:
        return f"{self.source_id}#c{self.index}"


def make_source(text: str, *, kind: Literal["pasted", "url"], title: str = "", url: str | None = None) -> Source:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return Source(id=uuid.uuid5(_NAMESPACE, digest), kind=kind, text=text, title=title, url=url, digest=digest)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in _SKIP:
            self._skip += 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in _SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """(title, readable text) of an HTML page: scripts, styles and markup removed, blocks on lines."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).splitlines()]
    text = "\n".join(line for line in lines if line)
    return " ".join("".join(parser.title).split()), text


def chunk_source(source: Source, *, words: int, overlap: int) -> list[Chunk]:
    """Windows of `words` words with `overlap` words shared, cut on word boundaries; a window
    prefers to end at a sentence end in its last quarter."""
    tokens = [(m.start(), m.end()) for m in _WORD.finditer(source.text)]
    if not tokens:
        return []
    chunks: list[Chunk] = []
    first = 0
    while first < len(tokens):
        last = min(len(tokens), first + words) - 1
        if last < len(tokens) - 1:
            floor = first + (3 * words) // 4
            for k in range(last, max(floor, first) - 1, -1):
                if source.text[tokens[k][1] - 1] in ".!?":
                    last = k
                    break
        start, end = tokens[first][0], tokens[last][1]
        chunks.append(Chunk(source.id, len(chunks), source.text[start:end], start, end))
        if last >= len(tokens) - 1:
            break
        first = max(first + 1, last + 1 - overlap)
    return chunks
