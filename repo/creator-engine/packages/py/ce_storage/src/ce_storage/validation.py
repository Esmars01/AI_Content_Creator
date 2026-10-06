"""Upload validation (§33): declared size, magic-byte sniffing, ffprobe in a subprocess with a
timeout, and rejection of unexpected containers. Pure CPU work; no model calls.

Used by `AssetValidationWorkflow` (Phase 2, ADR 0031) and by the API's upload checks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ce_core.errors import Issue

__all__ = ["KIND_FAMILIES", "MEDIA", "Media", "ValidationResult", "family_of", "sniff", "validate_file"]


def _iso_bmff(quicktime: bool) -> Callable[[bytes], bool]:
    def check(head: bytes) -> bool:
        if len(head) < 12 or head[4:8] != b"ftyp":
            return False
        return (head[8:12] == b"qt  ") == quicktime

    return check


def _riff(form: bytes) -> Callable[[bytes], bool]:
    return lambda head: head[:4] == b"RIFF" and head[8:12] == form


def _mp3(head: bytes) -> bool:
    return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


def _text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:  # a multi-byte character cut at the end of the sample is fine
        return exc.start >= len(head) - 3
    return True


@dataclass(frozen=True)
class Media:
    family: str  # image | video | audio | document
    sniff: Callable[[bytes], bool]
    formats: frozenset[str] = frozenset()  # accepted ffprobe format_name values; empty = no probe
    needs: str | None = None  # stream codec_type that must be present


_MP4 = frozenset({"mov,mp4,m4a,3gp,3g2,mj2"})
MEDIA: dict[str, Media] = {
    "video/mp4": Media("video", _iso_bmff(quicktime=False), _MP4, "video"),
    "video/quicktime": Media("video", _iso_bmff(quicktime=True), _MP4, "video"),
    "video/webm": Media("video", lambda h: h[:4] == b"\x1a\x45\xdf\xa3", frozenset({"matroska,webm"}), "video"),
    "audio/wav": Media("audio", _riff(b"WAVE"), frozenset({"wav"}), "audio"),
    "audio/x-wav": Media("audio", _riff(b"WAVE"), frozenset({"wav"}), "audio"),
    "audio/mpeg": Media("audio", _mp3, frozenset({"mp3"}), "audio"),
    "audio/flac": Media("audio", lambda h: h[:4] == b"fLaC", frozenset({"flac"}), "audio"),
    "image/png": Media("image", lambda h: h[:8] == b"\x89PNG\r\n\x1a\n", frozenset({"png_pipe"}), "video"),
    "image/jpeg": Media("image", lambda h: h[:3] == b"\xff\xd8\xff", frozenset({"jpeg_pipe"}), "video"),
    "image/webp": Media("image", _riff(b"WEBP"), frozenset({"webp_pipe"}), "video"),
    "application/pdf": Media("document", lambda h: h[:5] == b"%PDF-"),
    "text/plain": Media("document", _text),
    "text/markdown": Media("document", _text),
    # Phase 12 research documents: DOCX (a zip archive), HTML, and SRT/WebVTT transcripts
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": Media(
        "document", lambda h: h[:4] == b"PK\x03\x04"
    ),
    "text/html": Media("document", _text),
    "text/vtt": Media("document", lambda h: _text(h) and h.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT")),
    "application/x-subrip": Media("document", _text),
}

# Which media families each asset kind accepts (§29 `assets.kind`).
KIND_FAMILIES: dict[str, frozenset[str]] = {
    "image": frozenset({"image"}),
    "logo": frozenset({"image"}),
    "product": frozenset({"image"}),
    "video": frozenset({"video"}),
    "screen_recording": frozenset({"video"}),
    "audio": frozenset({"audio"}),
    "music": frozenset({"audio"}),
    "sfx": frozenset({"audio"}),
    "document": frozenset({"document"}),
    "reference": frozenset({"image", "video", "audio"}),
    "font": frozenset(),  # no font media types are accepted yet
}


def family_of(mime: str) -> str | None:
    media = MEDIA.get(mime)
    return media.family if media else None


def sniff(mime: str, head: bytes) -> bool:
    media = MEDIA.get(mime)
    return media is not None and media.sniff(head)


@dataclass
class ValidationResult:
    ok: bool
    sha256: str = ""
    size: int = 0
    probe: dict[str, Any] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)


def _compact_probe(raw: dict[str, Any]) -> dict[str, Any]:
    fmt = raw.get("format", {})
    keep_stream = ("index", "codec_type", "codec_name", "width", "height", "r_frame_rate", "sample_rate", "channels")
    return {
        "format": {k: fmt.get(k) for k in ("format_name", "duration", "bit_rate", "nb_streams") if k in fmt},
        "streams": [{k: s[k] for k in (*keep_stream, "duration") if k in s} for s in raw.get("streams", [])],
    }


async def _ffprobe(path: Path, timeout_s: float) -> dict[str, Any]:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise RuntimeError("ffprobe is not installed")
    process = await asyncio.create_subprocess_exec(
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    errors = stderr.decode("utf-8", "replace").strip()
    if process.returncode != 0 or errors:
        # At `-v error` ffprobe only prints real decoding errors; it still exits 0 on many of them.
        raise ValueError(errors[:300] or f"ffprobe exited with {process.returncode}")
    data: dict[str, Any] = json.loads(stdout or b"{}")
    return data


async def validate_file(path: Path, *, mime: str, declared_bytes: int, ffprobe_timeout_s: float) -> ValidationResult:
    """Check an uploaded file against what the client declared."""
    result = ValidationResult(ok=False)
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        head = handle.read(4096)
        sha.update(head)
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    result.sha256 = sha.hexdigest()
    result.size = path.stat().st_size

    def reject(code: str, message: str, **detail: Any) -> ValidationResult:
        result.issues.append(Issue(code, message, detail=detail))
        return result

    if result.size != declared_bytes:
        return reject(
            "upload_size", "the uploaded size differs from the declared size", got=result.size, declared=declared_bytes
        )
    media = MEDIA.get(mime)
    if media is None:
        return reject("upload_media_type", f"{mime} is not accepted")
    if not media.sniff(head):
        return reject("upload_sniff", f"the file content is not {mime}")
    if not media.formats:
        result.ok = True
        return result
    try:
        raw = await _ffprobe(path, ffprobe_timeout_s)
    except TimeoutError:
        return reject("upload_probe_timeout", f"ffprobe did not finish within {ffprobe_timeout_s} s")
    except ValueError as exc:
        return reject("upload_probe", "ffprobe could not read the file", error=str(exc))
    result.probe = _compact_probe(raw)
    format_name = str(raw.get("format", {}).get("format_name", ""))
    if format_name not in media.formats:
        return reject("upload_container", f"unexpected container {format_name!r} for {mime}", format=format_name)
    types = {s.get("codec_type") for s in raw.get("streams", [])}
    if media.needs and media.needs not in types:
        return reject("upload_streams", f"{mime} needs a {media.needs} stream", streams=sorted(t for t in types if t))
    if media.family in ("image", "video"):
        visual = [s for s in raw.get("streams", []) if s.get("codec_type") == "video"]
        if not any(int(s.get("width") or 0) > 0 and int(s.get("height") or 0) > 0 for s in visual):
            return reject("upload_streams", "no decodable picture (zero width or height)")
    result.ok = True
    return result
