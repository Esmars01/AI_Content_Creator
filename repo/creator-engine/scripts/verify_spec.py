#!/usr/bin/env python3
"""Mechanical consistency checks for docs/MASTER_BUILD_PROMPT.md.

Checks that every cross-reference in the spec resolves to something the spec
defines: sections, subsections, phases, invariants, ADRs, API paths, tables,
workflows, job kinds, node kinds, capabilities, config files, env vars,
Makefile targets, and that every embedded JSON example parses.

Exit code 0 = no errors (warnings are printed but do not fail).
Usage: python scripts/verify_spec.py [path]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Dotted tokens that are spec field paths, table.column references or SSE event
# types rather than node kinds or capabilities.
ALLOWED_DOTTED = {
    "audio.loudness",
    "audio.mic_profile",
    "provenance.visible_label",
    "captions.review_state",
    "qc.flagged",
    "render.ready",
}
# Env-like tokens the spec mentions only to say they do not exist.
ALLOWED_ENV_MENTIONS = {"MOCK_LLM"}

SPEC = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/MASTER_BUILD_PROMPT.md")


def section_bodies(text: str) -> dict[str, str]:
    """Map top-level section number -> body text."""
    parts = re.split(r"^## (\d+)\. .*$", text, flags=re.M)
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        out[parts[i]] = parts[i + 1]
    return out


def main() -> int:
    text = SPEC.read_text(encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []
    secs = section_bodies(text)
    heads = set(secs)
    subs = set(re.findall(r"^### (\d+\.\d+)", text, re.M))

    # 1. Section references.
    for m in re.finditer(r"(?:§|sections? )(\d+(?:\.\d+)?)", text):
        ref = m.group(1)
        ok = ref in subs if "." in ref else ref in heads
        if not ok:
            errors.append(f"unresolved section reference '{m.group(0)}'")
    for m in re.finditer(r"\((\d{1,2}\.\d{1,2})(?:[,)])", text):
        ref = m.group(1)
        if ref not in subs and not re.match(r"0\.\d", ref):
            warnings.append(f"possible bare subsection reference ({ref}) not found")

    # 2. Phases.
    phases = set(re.findall(r"^### Phase (\d+) ", text, re.M))
    for m in re.finditer(r"Phases? (\d+)(?:[\u2013-](\d+))?", text):
        for p in (m.group(1), m.group(2)):
            if p and p not in phases:
                errors.append(f"reference to undefined Phase {p}")

    # 3. Invariants.
    invariants = set(re.findall(r"^\| (I\d+) \|", secs.get("4", ""), re.M))
    for m in re.finditer(r"\b(I\d{1,2})\b", text):
        if m.group(1) not in invariants:
            errors.append(f"reference to undefined invariant {m.group(1)}")
    inv_phase_line = re.search(r"\*\*Invariants\*\* \(`tests/invariants/`\):(.*)", text)
    if inv_phase_line:
        named = set(re.findall(r"\bI(\d+)\b", inv_phase_line.group(1)))
        for inv in invariants:
            if inv[1:] not in named:
                errors.append(f"invariant {inv} has no phase in the §37 invariant schedule")

    # 4. ADRs.
    adrs = set(re.findall(r"^\| (\d{4}) \|", secs.get("6", ""), re.M))
    for m in re.finditer(r"ADRs? (\d{4})(?:[\u2013-](\d{4}))?", text):
        for a in (m.group(1), m.group(2)):
            if a and a not in adrs:
                errors.append(f"reference to undefined ADR {a}")
    if len(adrs) != 27:
        errors.append(f"expected 27 ADRs in §6, found {len(adrs)}")

    # 5. API paths: every /v1 or /internal path in prose must exist in §30.
    def norm(path: str) -> str:
        path = path.split("?")[0].rstrip("`.,;)")
        path = re.sub(r"\{[^}]+\}", "{}", path)
        return path

    api_text = secs.get("30", "")
    api_paths: set[str] = set()
    for m in re.finditer(r"(/(?:v1|internal)/[A-Za-z0-9_\-/{}.:|…]+)", api_text):
        raw = m.group(1)
        base, _, verbs = raw.partition(":")
        variants = [base] if not verbs else [f"{base}:{v}" for v in verbs.split("|")]
        for v in variants:
            api_paths.add(norm(v))
    # Paths written with "…/" shorthand inherit the previous full path's prefix;
    # accept them by suffix.
    for name, body in secs.items():
        if name == "30":
            continue
        for m in re.finditer(r"(/(?:v1|internal)/[A-Za-z0-9_\-/{}.:|]+)", body):
            raw = m.group(1)
            base, _, verbs = raw.partition(":")
            variants = [base] if not verbs else [f"{base}:{v}" for v in verbs.split("|")]
            for v in variants:
                n = norm(v)
                if n.endswith("/*") or n.endswith("/") or n in api_paths:
                    continue
                if any(p.endswith(n.split("/v1", 1)[-1]) for p in api_paths):
                    continue
                errors.append(f"§{name}: API path {raw} not defined in §30")

    # 6. Tables: references of the form `name` table / rows in `name` must exist in §29.
    tables = set(re.findall(r"^\| `([a-z_]+)` \|", secs.get("29", ""), re.M))
    tables.add("gpu_tasks")
    ref_tables: set[str] = set()
    for pat in (r"`([a-z_]+)` (?:table|rows?)\b", r"\btables? `([a-z_]+)`"):
        ref_tables.update(re.findall(pat, text))
    known_non_tables = {"ce_core", "ce_build", "ce_config", "ce_worker", "ce_voice", "ce_policy", "ce_qc"}
    for t in sorted(ref_tables - known_non_tables):
        if (
            t not in tables
            and not t.startswith("ce_")
            and t
            not in {
                "intent",
                "scenes",
                "cast",
                "states",
                "spec",
                "render",
                "audio",
                "brief",
                "meta",
                "memory",
                "generation",
                "captions",
                "script",
                "shots",
                "world",
            }
        ):
            warnings.append(f"table-like reference `{t}` not found in §29 (check manually)")
    for t in (
        "build_manifest_entries",
        "cache_entries",
        "artifact_refs",
        "creator_memory_items",
        "memory_snapshots",
        "creator_usage_events",
        "world_versions",
        "behavior_observations",
        "model_behavior_profiles",
        "voice_candidates",
        "human_ratings",
        "consistency_reports",
        "creator_baselines",
    ):
        if t not in tables:
            errors.append(f"expected table {t} missing from §29")

    # 7. Workflows and job kinds.
    wf_rows = re.findall(r"^\| `([a-z_ /`]+)` \| `([A-Za-z /`]+)` \|", secs.get("9", ""), re.M)
    workflows: set[str] = set()
    for _, w in wf_rows:
        workflows.update(re.findall(r"([A-Z][A-Za-z]+Workflow)", w))
    workflows.add("SceneBuildWorkflow")
    for m in set(re.findall(r"\b([A-Z][A-Za-z]+Workflow)\b", text)):
        if m not in workflows:
            errors.append(f"workflow {m} not in the §9 job-kind table")

    # 8. Node kinds in the §12.1 table vs mentions of node-like tokens.
    node_table = re.search(r"### 12\.1(.*?)### 12\.2", text, re.S)
    if node_table is None:
        errors.append("§12.1 node table not found")
    node_kinds = set(re.findall(r"^\| `([a-z_]+\.[a-z_]+)`", node_table.group(1) if node_table else "", re.M))
    node_kinds.update(
        {
            "provenance.watermark_video",
            "provenance.watermark_audio",
            "provenance.sign",
            "video.upscale",
            "video.interpolate",
            "post.camera",
            "post.realism",
        }
    )
    caps_text = secs.get("23", "")
    capabilities = set(re.findall(r"`([a-z_]+\.[a-z_0-9]+)`", caps_text))
    for name, body in secs.items():
        if name in {"8", "23"}:
            continue
        for m in re.finditer(
            r"`((?:behavior|world|image|avatar|lipsync|video|screen|qc|post|audio|captions|mix|render|provenance|tts|asr|align|voice)\.[a-z_*]+)`",
            body,
        ):
            tok = m.group(1)
            if tok in node_kinds or tok in capabilities or tok.endswith("*") or tok in ALLOWED_DOTTED:
                continue
            warnings.append(f"§{name}: dotted token `{tok}` is neither a node kind nor a capability")

    # 9. Config files referenced must appear in the §8 layout.
    layout = secs.get("8", "")
    for m in set(re.findall(r"`(config/[A-Za-z0-9_\-/.*{},<>]+)`", text)):
        leaf = m.split("/")[-1].replace("*", "").replace("{", "").replace("}", "")
        stem = m.split("/")[1]
        if stem.rstrip("/") not in layout and leaf not in layout:
            errors.append(f"config path {m} not in the §8 layout")

    # 10. Env vars used in prose must be in the §35 block.
    env_block = re.search(r"```\nAPP_ENV=.*?```", text, re.S)
    env_vars = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", env_block.group(0), re.M)) if env_block else set()
    for m in set(re.findall(r"`([A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+)(?:=[^`]*)?`", text)):
        if (
            m not in env_vars
            and m not in ALLOWED_ENV_MENTIONS
            and not m.startswith(("HONORED", "APPROXIMATED", "UNSUPPORTED", "NOT_", "FAILED"))
        ):
            warnings.append(f"env-like token `{m}` not in the §35 env block")

    # 11. Makefile targets.
    targets = set()
    tline = re.search(r"\*\*Makefile targets\*\*: (.*)", text)
    if tline:
        targets = set(re.findall(r"`([a-z0-9\-]+)", tline.group(1)))
    for m in set(re.findall(r"`make ([a-z0-9\-]+)", text)):
        if m not in targets:
            errors.append(f"make target {m} not in the §36 list")

    # 12. JSON examples parse.
    for i, m in enumerate(re.finditer(r"```json\n(.*?)\n```", text, re.S)):
        try:
            json.loads(m.group(1))
        except json.JSONDecodeError as exc:
            errors.append(f"JSON block {i} does not parse: {exc}")

    # (The §38 docs are not checked here: their existence is checked at the end of each phase.)

    print(f"spec: {SPEC} ({len(text.split())} words)")
    print(
        f"sections={len(heads)} subsections={len(subs)} phases={len(phases)} invariants={len(invariants)} "
        f"adrs={len(adrs)} tables={len(tables)} workflows={len(workflows)} node_kinds={len(node_kinds)} "
        f"capabilities={len(capabilities)} env_vars={len(env_vars)} make_targets={len(targets)} "
        f"api_paths={len(api_paths)}"
    )
    for w in sorted(set(warnings)):
        print("WARN ", w)
    for e in sorted(set(errors)):
        print("ERROR", e)
    print(f"{len(set(errors))} errors, {len(set(warnings))} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
