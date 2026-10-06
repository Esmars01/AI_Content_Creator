"""A generated two-scene video in the dev organization, for the Playwright edit test.

`uv run python scripts/e2e_video.py` submits the two-scene fixture (`two_scene_spec_dict`, creator
Alex from `ce seed dev`, the voice locked) as an approved version, waits until the running stack
(`make dev`) has built it, and prints `{"video_id": …, "version_id": …, "state": …}` on its last
line. A fresh seed namespace keeps the run independent of earlier caches. Test plumbing only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


async def main() -> int:
    from ce_api.video_cli import generate_fixture
    from e2e_user import load_env

    load_env()
    summary = await generate_fixture(
        ROOT / "config",
        wait=True,
        out=None,
        seed_namespace=str(uuid.uuid4()),
        timeout_s=600,
        fixture="two_scene",
    )
    print(json.dumps({k: summary.get(k) for k in ("video_id", "version_id", "state", "seconds")}))
    return 0 if summary.get("state") == "ready" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
