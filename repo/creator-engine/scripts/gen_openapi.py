#!/usr/bin/env python3
"""Exports the API's OpenAPI document (§30) to packages/ts/api-client/schema/api.openapi.json.

`make gen-client` runs this, then openapi-typescript turns it into src/api.ts. It also renders
the endpoint table of docs/API.md. `--check` compares instead of writing and exits 1 when either
committed file is stale.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ce_api.app import create_app

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "packages" / "ts" / "api-client" / "schema" / "api.openapi.json"
DOC = ROOT / "docs" / "API.md"
BEGIN = "<!-- generated:endpoints (scripts/gen_openapi.py) -->"
END = "<!-- end:endpoints -->"


def render(spec: dict[str, Any]) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_doc(spec: dict[str, Any], current: str) -> str:
    rows = ["| Method | Path | Summary | Success |", "| --- | --- | --- | --- |"]
    by_tag: dict[str, list[str]] = {}
    for path, operations in sorted(spec["paths"].items()):
        for method, op in operations.items():
            success = ", ".join(sorted(s for s in op["responses"] if s.startswith("2")))
            tag = (op.get("tags") or ["other"])[0]
            row = f"| `{method.upper()}` | `{path}` | {op.get('summary', '')} | {success} |"
            by_tag.setdefault(tag, []).append(row)
    body = []
    for tag in sorted(by_tag):
        body += [f"### {tag}", "", *rows, *by_tag[tag], ""]
    head, _, rest = current.partition(BEGIN)
    _, _, tail = rest.partition(END)
    return f"{head}{BEGIN}\n\n" + "\n".join(body) + f"{END}{tail}"


def main(argv: list[str]) -> int:
    spec = create_app().openapi()
    targets = {OUT: render(spec), DOC: render_doc(spec, DOC.read_text(encoding="utf-8"))}
    if "--check" in argv:
        stale = [p for p, text in targets.items() if (p.read_text(encoding="utf-8") if p.exists() else "") != text]
        for path in stale:
            print(f"{path.relative_to(ROOT)} is stale; run `make gen-client`")
        if not stale:
            print("OpenAPI document and API.md endpoint table are fresh")
        return 1 if stale else 0
    for path, text in targets.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
