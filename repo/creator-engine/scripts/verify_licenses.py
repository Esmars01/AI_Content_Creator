#!/usr/bin/env python3
"""Re-checks every license block of every installed plugin manifest (rule 15, §24; Phase 8).

Offline (always): every model and dependency has a license block with `verified_at`/`verified_by`; a
Hugging Face or GitHub license URL names the pinned revision (a `blob/<sha>/` path), so it reads the text
at the pin rather than whatever the branch says today; non-commercial weights are flagged as such and are
never a production default (`status: production` with `commercial_use: false` is an error).

Online (`--online`): fetches the evidence again at the pinned revision and compares:
- `text_sha256` set → the license file's bytes at the pinned URL must hash to it;
- `evidence: model_card…` on a Hugging Face repo → the card metadata (`cardData.license`) at the pinned
  revision must name the recorded license;
- a `pypi:<name>==<version>` dependency → the release's PyPI metadata (license expression, license
  field or classifiers) must name the recorded license.
Hosts that answer with an error are reported as `unreachable`, not as mismatches.

Exit status: 1 on any error or mismatch (unreachable evidence is a warning unless `--strict`).
Usage: uv run python scripts/verify_licenses.py [--online] [--strict] [--json PATH] [--plugin ID]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
# A pinned revision: a commit sha, or a release tag (`v0.40.0`, `1.2.1`), never a branch name.
_REV = r"(?P<rev>[0-9a-f]{7,40}|v?\d+(?:\.\d+)+[\w.-]*)"
HF_BLOB = re.compile(r"^https://huggingface\.co/(?P<repo>[^/]+/[^/]+)/blob/" + _REV + r"/(?P<path>.+)$")
GH_BLOB = re.compile(r"^https://github\.com/(?P<repo>[^/]+/[^/]+)/blob/" + _REV + r"/(?P<path>.+)$")
PYPI_REF = re.compile(r"^pypi:(?P<name>[A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(?P<version>[\w.+-]+)")
# Normalized license names for comparing SPDX ids, card metadata and PyPI classifiers.
ALIASES = {
    "apache-2.0": {"apache-2.0", "apache 2.0", "apache license 2.0", "apache software license", "apache"},
    "mit": {"mit", "mit license"},
    "bsd-3-clause": {"bsd-3-clause", "bsd", "bsd license"},
    "cc-by-4.0": {"cc-by-4.0"},
    "creativeml-openrail-m": {"creativeml-openrail-m"},
    "isc": {"isc", "isc license"},
    "gpl-3.0": {"gpl-3.0", "gpl v3", "gplv3", "gnu general public license v3 (gplv3)"},
}


# License texts (PyPI `license` fields often hold the whole text) → the canonical id.
TEXT_MARKERS = (
    ("apache license, version 2.0", "apache-2.0"),
    ("apache license\n", "apache-2.0"),
    ("gnu general public license\n                               version 3", "gpl-3.0"),
    ("gnu general public license version 3", "gpl-3.0"),
    ("permission is hereby granted, free of charge", "mit"),
    ("redistribution and use in source and binary forms", "bsd-3-clause"),
    ("permission to use, copy, modify, and/or distribute this software", "isc"),
)


def _norm(name: str) -> str:
    text = " ".join(name.strip().lower().replace("license :: osi approved :: ", "").split())
    if len(text) > 80:  # a license text, not a name
        lowered = name.lower()
        for marker, canonical in TEXT_MARKERS:
            if " ".join(marker.split()) in " ".join(lowered.split()):
                return canonical
        return text[:80]
    text = text.removesuffix("-or-later").removesuffix("-only").replace(" license", "")
    if text.startswith(("gpl-3", "gplv3", "gnu general public license v3", "gnu general public")):
        return "gpl-3.0"
    for canonical, names in ALIASES.items():
        if text in names or text == canonical:
            return canonical
    return text


@dataclass
class Finding:
    plugin: str
    ref: str
    level: str  # error | mismatch | unreachable | ok | warning
    message: str


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0

    def add(self, *args: str) -> None:
        self.findings.append(Finding(*args))


def blocks(manifest: Any) -> list[tuple[str, Any, Any]]:
    out = []
    for decl in manifest.models:
        out.append((decl.key, decl.license, decl.source))
        out += [(dep.ref or dep.role or "dependency", dep.license, dep.source) for dep in decl.dependencies]
    return out


def offline(plugin_id: str, manifest: Any, report: Report) -> None:
    for ref, lic, _source in blocks(manifest):
        report.checked += 1
        if not lic.verified_by or not lic.verified_at:
            report.add(plugin_id, ref, "error", "license block without verified_at/verified_by")
        pinned_by_pypi = PYPI_REF.match(ref) is not None  # the PyPI release is the pin (checked online)
        for pattern, host in ((HF_BLOB, "huggingface.co"), (GH_BLOB, "github.com")):
            if host in lic.url and "/blob/" in lic.url and not pattern.match(lic.url) and not pinned_by_pypi:
                report.add(plugin_id, ref, "error", f"{lic.url} does not name a pinned revision")
        unpinned = lic.url.startswith(("https://github.com/", "https://huggingface.co/")) and "/blob/" not in lic.url
        if unpinned and lic.text_sha256:
            report.add(plugin_id, ref, "error", "text_sha256 recorded for an unpinned URL")
        if not lic.commercial_use and manifest.status == "production":
            report.add(plugin_id, ref, "error", "non-commercial license on a production plugin (rule 15)")
        if not lic.commercial_use and manifest.status != "production":
            report.add(plugin_id, ref, "warning", f"non-commercial ({lic.name}): sandbox only")


def _get(client: Any, url: str) -> tuple[int, bytes]:
    try:
        response = client.get(url, follow_redirects=True)
    except Exception as exc:  # network failures are "unreachable", never a mismatch
        return 0, str(exc).encode()
    return response.status_code, response.content


def _raw(url: str) -> str | None:
    hf, gh = HF_BLOB.match(url), GH_BLOB.match(url)
    if hf:
        return f"https://huggingface.co/{hf['repo']}/resolve/{hf['rev']}/{hf['path']}"
    if gh:
        return f"https://raw.githubusercontent.com/{gh['repo']}/{gh['rev']}/{gh['path']}"
    return None


def online(plugin_id: str, manifest: Any, report: Report, client: Any) -> None:
    for ref, lic, source in blocks(manifest):
        if lic.text_sha256:
            raw = _raw(lic.url)
            if raw is None:
                report.add(plugin_id, ref, "warning", f"text_sha256 cannot be re-fetched from {lic.url} (no file URL)")
            else:
                status, body = _get(client, raw)
                if status != 200:
                    report.add(plugin_id, ref, "unreachable", f"{raw}: HTTP {status}")
                elif hashlib.sha256(body).hexdigest() != lic.text_sha256:
                    report.add(plugin_id, ref, "mismatch", f"license text at {raw} no longer hashes to text_sha256")
                else:
                    report.add(plugin_id, ref, "ok", "license text matches text_sha256")
        if lic.evidence.startswith("model_card") and source is not None and source.type == "huggingface":
            api = f"https://huggingface.co/api/models/{source.repo}/revision/{source.revision}"
            status, body = _get(client, api)
            if status != 200:
                report.add(plugin_id, ref, "unreachable", f"{api}: HTTP {status}")
            else:
                card = (json.loads(body).get("cardData") or {}).get("license")
                names = card if isinstance(card, list) else [card]
                if card is None:
                    report.add(plugin_id, ref, "mismatch", "the model card at the pin declares no license")
                elif _norm(lic.name) not in {_norm(str(n)) for n in names}:
                    report.add(plugin_id, ref, "mismatch", f"model card says {card!r}, the block says {lic.name!r}")
                else:
                    report.add(plugin_id, ref, "ok", f"model card license {card!r} at the pin")
        pypi = PYPI_REF.match(ref)
        if pypi:
            url = f"https://pypi.org/pypi/{pypi['name']}/{pypi['version']}/json"
            status, body = _get(client, url)
            if status != 200:
                report.add(plugin_id, ref, "unreachable", f"{url}: HTTP {status}")
                continue
            info = json.loads(body).get("info", {})
            candidates = [str(info.get("license_expression") or ""), str(info.get("license") or "")]
            candidates += [c.split(" :: ")[-1] for c in info.get("classifiers", []) if c.startswith("License ::")]
            found = {_norm(c) for c in candidates if c.strip()}
            wanted = {_norm(part) for part in re.split(r"\s+OR\s+", lic.name)}
            if not found:
                report.add(
                    plugin_id, ref, "warning", "PyPI metadata declares no license (the block's URL is the evidence)"
                )
            elif wanted & found:
                report.add(plugin_id, ref, "ok", "PyPI metadata names the recorded license")
            else:
                report.add(
                    plugin_id, ref, "mismatch", f"PyPI metadata says {sorted(found)}, the block says {lic.name!r}"
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--online", action="store_true", help="fetch the evidence again at the pinned revisions")
    parser.add_argument("--strict", action="store_true", help="unreachable evidence is an error")
    parser.add_argument("--json", type=Path, help="write the findings here")
    parser.add_argument("--plugin", action="append", default=[], help="only these plugin ids")
    args = parser.parse_args()
    from ce_contracts.plugins import discover

    registry = discover(app_env=None, include_mocks=False)
    report = Report()
    client = None
    if args.online:
        import httpx

        client = httpx.Client(timeout=30, headers={"User-Agent": "creator-engine-license-check"})
    for plugin_id, plugin in sorted(registry.plugins.items()):
        if args.plugin and plugin_id not in args.plugin:
            continue
        manifest = plugin.manifest
        if manifest.kind == "provider" and not manifest.models:
            continue
        offline(plugin_id, manifest, report)
        if client is not None:
            online(plugin_id, manifest, report, client)
    failing = {"error", "mismatch"} | ({"unreachable"} if args.strict else set())
    counts: dict[str, int] = {}
    for f in report.findings:
        counts[f.level] = counts.get(f.level, 0) + 1
        if f.level != "ok":
            print(f"{f.level:11} {f.plugin}: {f.ref}: {f.message}")
    print(f"{report.checked} license blocks; " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    if args.json:
        args.json.write_text(json.dumps([f.__dict__ for f in report.findings], indent=2) + "\n", encoding="utf-8")
    return 1 if any(f.level in failing for f in report.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
