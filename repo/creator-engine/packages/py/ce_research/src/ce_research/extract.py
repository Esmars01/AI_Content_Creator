"""Text extraction for persistent research sources (Phase 12, §13 stage 3, §29 `research_sources`).

Every extractor turns untrusted bytes into plain text and **never interprets the content**: what a
document says is data for the fact check, never an instruction (I10). Extraction is deterministic
and bounded (pages, characters, uncompressed archive size), so a hostile file costs a refusal, not
the worker.

| kind | accepted | how |
| --- | --- | --- |
| `url` | text/html, text/plain, text/markdown, application/pdf (by content type) | the fetched body, as below |
| `pdf` | application/pdf | `pypdf` text per page (BSD-3-Clause); encrypted files are refused |
| `doc` | DOCX (zip + WordprocessingML), text/plain, text/markdown, text/html | stdlib zip/XML for DOCX |
| `transcript` | SRT, WebVTT or plain text | cue numbers and timings dropped; cue text kept in order |
| `note` | text/plain, text/markdown | as is |

`video` sources (a transcript produced by ASR) are not extracted here; the API refuses them until
an ASR-backed ingestion exists (it would be a model call, never done synchronously).
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree

from ce_research.documents import html_to_text

__all__ = [
    "ExtractError",
    "ExtractLimits",
    "Extracted",
    "extract",
    "kind_for_mime",
    "parse_transcript",
]

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TIMING = re.compile(r"^\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(?:\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}.*$")
_CUE_NUMBER = re.compile(r"^\s*\d+\s*$")
_TAG = re.compile(r"<[^>]{1,200}>")
# C0/C1 controls except tab and newline: never meaningful in research text.
_CONTROLS = {c: None for c in range(0x20) if c not in (0x09, 0x0A)} | {c: None for c in range(0x7F, 0xA0)}


class ExtractError(Exception):
    """The document cannot be extracted (wrong type, encrypted, too large, malformed)."""


@dataclass(frozen=True)
class ExtractLimits:
    max_chars: int = 2_000_000
    max_pdf_pages: int = 500
    max_archive_bytes: int = 50 * 1024**2  # uncompressed DOCX parts read


@dataclass(frozen=True)
class Extracted:
    text: str
    title: str = ""
    mime: str = ""
    pages: int | None = None
    warnings: list[str] = field(default_factory=list)


def _clean(text: str, limits: ExtractLimits, warnings: list[str]) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").translate(_CONTROLS)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    cleaned = "\n".join(out).strip()
    if len(cleaned) > limits.max_chars:
        warnings.append(f"text truncated to {limits.max_chars} characters")
        cleaned = cleaned[: limits.max_chars]
    return cleaned


def kind_for_mime(mime: str) -> str | None:
    """The source kind an upload of this media type belongs to (None: not supported)."""
    mime = mime.split(";")[0].strip().lower()
    if mime == "application/pdf":
        return "pdf"
    if mime in (_DOCX, "text/html", "application/xhtml+xml"):
        return "doc"
    if mime in ("text/vtt", "application/x-subrip", "text/srt"):
        return "transcript"
    if mime in ("text/plain", "text/markdown"):
        return "note"
    return None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _pdf(data: bytes, limits: ExtractLimits, warnings: list[str]) -> tuple[str, str, int]:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise ExtractError("PDF support is not installed") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ExtractError("the PDF is encrypted")
        pages = len(reader.pages)
        texts: list[str] = []
        for index, page in enumerate(reader.pages):
            if index >= limits.max_pdf_pages:
                warnings.append(f"only the first {limits.max_pdf_pages} of {pages} pages were read")
                break
            texts.append(page.extract_text() or "")
        meta = reader.metadata
        title = str(meta.title) if meta is not None and meta.title else ""
    except ExtractError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError, OSError) as exc:
        raise ExtractError(f"the PDF cannot be read: {type(exc).__name__}") from exc
    if not any(t.strip() for t in texts):
        warnings.append("the PDF has no text layer (scanned pages need OCR, which is not run here)")
    return "\n\n".join(texts), title, pages


def _docx(data: bytes, limits: ExtractLimits) -> tuple[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ExtractError("the document is not a DOCX (zip) file") from exc
    with archive:
        names = set(archive.namelist())
        if "word/document.xml" not in names:
            raise ExtractError("the archive has no word/document.xml")
        info = archive.getinfo("word/document.xml")
        if info.file_size > limits.max_archive_bytes:
            raise ExtractError("the document body is too large once uncompressed")
        with archive.open(info) as handle:
            body = handle.read(limits.max_archive_bytes + 1)
        if len(body) > limits.max_archive_bytes:
            raise ExtractError("the document body is too large once uncompressed")
        title = ""
        if "docProps/core.xml" in names and archive.getinfo("docProps/core.xml").file_size < 1_000_000:
            core = ElementTree.fromstring(archive.read("docProps/core.xml"))  # noqa: S314 - see _docx
            node = core.find("{http://purl.org/dc/elements/1.1/}title")
            title = (node.text or "").strip() if node is not None else ""
    try:
        # ElementTree never resolves external entities, and the bundled expat (≥ 2.4) caps entity
        # expansion (billion laughs); the body size is capped above.
        root = ElementTree.fromstring(body)  # noqa: S314
    except ElementTree.ParseError as exc:
        raise ExtractError("the document XML is malformed") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs), title


def parse_transcript(text: str) -> list[str]:
    """Cue texts of an SRT/WebVTT file in order (numbers, timings, headers and markup dropped);
    plain text passes through line by line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    vtt = bool(lines) and lines[0].lstrip("﻿").startswith("WEBVTT")
    out: list[str] = []
    skip_block = False
    for i, raw in enumerate(lines):
        line = raw.strip()
        if vtt and i == 0:
            continue
        if not line:
            skip_block = False
            continue
        if vtt and (line.startswith(("NOTE", "STYLE", "REGION"))):
            skip_block = True
            continue
        if skip_block or _TIMING.match(line) or _CUE_NUMBER.match(line):
            continue
        cleaned = _TAG.sub("", line).strip()
        if cleaned:
            out.append(cleaned)
    return out


def extract(data: bytes, *, kind: str, mime: str, limits: ExtractLimits | None = None) -> Extracted:
    """Plain text of one source document (see the module table)."""
    limits = limits or ExtractLimits()
    mime = mime.split(";")[0].strip().lower()
    warnings: list[str] = []
    title, pages = "", None
    if kind == "video":
        raise ExtractError("video sources need speech recognition; upload a transcript instead")
    if mime == "application/pdf" and kind in ("pdf", "url", "doc"):
        raw, title, pages = _pdf(data, limits, warnings)
    elif mime == _DOCX and kind in ("doc", "url"):
        raw, title = _docx(data, limits)
    elif mime in ("text/html", "application/xhtml+xml") and kind in ("url", "doc", "note"):
        title, raw = html_to_text(_decode(data))
    elif mime in ("text/vtt", "application/x-subrip", "text/srt") or (kind == "transcript" and mime == "text/plain"):
        raw = "\n".join(parse_transcript(_decode(data)))
    elif mime in ("text/plain", "text/markdown") and kind in ("url", "doc", "note", "transcript"):
        raw = _decode(data)
    else:
        raise ExtractError(f"a {kind} source cannot be read from {mime or 'an unknown type'}")
    text = _clean(raw, limits, warnings)
    if not text:
        raise ExtractError("the document contains no text")
    return Extracted(text=text, title=" ".join(title.split())[:300], mime=mime, pages=pages, warnings=warnings)
