"""Deterministic placeholder media for the dev seed (rule 5: they are labelled placeholders).

Pure Python (zlib + struct), so the seed needs no image or audio library: a flat-colour PNG whose
colour is derived from the label, a silent mono 16-bit WAV, and minimal PDF / DOCX documents with
a text layer (research ingestion tests).
"""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib

__all__ = ["placeholder_docx", "placeholder_pdf", "placeholder_png", "placeholder_wav"]


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def placeholder_png(label: str, width: int = 64, height: int = 64) -> bytes:
    r, g, b = hashlib.sha256(label.encode("utf-8")).digest()[:3]
    row = b"\x00" + bytes((r, g, b)) * width  # filter type 0 per scanline
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")
    )


def placeholder_wav(seconds: float = 1.0, sample_rate: int = 24_000) -> bytes:
    frames = int(seconds * sample_rate)
    data = b"\x00\x00" * frames
    fmt = struct.pack("<HHIIHH", 1, 1, sample_rate, sample_rate * 2, 2, 16)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def placeholder_pdf(lines: list[str]) -> bytes:
    """A minimal valid one-page PDF with a text layer (Helvetica, one line per entry)."""
    ops = "BT /F1 12 Tf 72 720 Td " + " ".join(f"({line}) Tj 0 -16 Td" for line in lines) + " ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(ops) + ops.encode("latin-1") + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


def placeholder_docx(paragraphs: list[str], title: str = "") -> bytes:
    """A minimal DOCX (word/document.xml, optional docProps/core.xml title)."""
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
        if title:
            archive.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="x" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{title}</dc:title></cp:coreProperties>",
            )
    return buffer.getvalue()
