"""Where the repository's documentation is found (tests and developer scripts only).

The engine's own documentation tree (`docs/` next to this repository's sources: the spec as
`MASTER_BUILD_PROMPT.md`, ADRs, DECISIONS, INVARIANTS, OPERATIONS, API, MODELS, …) is present in a
plain checkout of the phase history. In the `AI_Content_Creator` workspace layout the sources live
in `repo/creator-engine/` and the canonical current documentation is the workspace's outer `docs/`,
which carries the spec as `MASTER_BUILD_PROMPT_v2_private.md` (the same document before erratum E1,
which only rewords invariant I3) and none of the historical per-topic documents; those stay in the
phase bundles.

Lookup order: `CE_DOCS_DIR` (point it at a bundle checkout's `docs/` to run every documentation
check), then `<repo>/docs`, then the workspace's outer `docs/`. A check whose document is in none of
them is skipped with that reason, never passed silently.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]

# The spec's file name in a plain checkout, then its name in the workspace's outer docs/.
SPEC_NAMES = ("MASTER_BUILD_PROMPT.md", "MASTER_BUILD_PROMPT_v2_private.md")


def docs_dirs() -> list[Path]:
    dirs: list[Path] = []
    if env := os.environ.get("CE_DOCS_DIR"):
        dirs.append(Path(env).resolve())
    dirs.append(ROOT / "docs")
    # AI_Content_Creator/repo/creator-engine -> AI_Content_Creator/docs
    if ROOT.parent.name == "repo":
        dirs.append(ROOT.parent.parent / "docs")
    return [d for d in dirs if d.is_dir()]


def find(*names: str) -> Path | None:
    """The first existing `docs/<name>` over the lookup order, trying each name in turn per directory."""
    for directory in docs_dirs():
        for name in names:
            path = directory / name
            if path.exists():
                return path
    return None


def spec_path() -> Path | None:
    return find(*SPEC_NAMES)


def missing_reason(*names: str) -> str:
    looked = ", ".join(str(d) for d in docs_dirs()) or "no docs directory"
    return (
        f"{' / '.join(names)} not found (looked in {looked}); in the AI_Content_Creator workspace this "
        "document lives only in the phase bundles — set CE_DOCS_DIR to a bundle checkout's docs/ to run it"
    )


def require(*names: str) -> Path:
    """The document's path, or skip the calling test with the reason it is absent."""
    import pytest

    path = find(*names)
    if path is None:
        pytest.skip(missing_reason(*names), allow_module_level=True)
    return path


def require_spec() -> Path:
    return require(*SPEC_NAMES)
