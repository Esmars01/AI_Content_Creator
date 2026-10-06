"""The mock-mode demo (`make demo`, Phase 14): the whole product path through the public HTTP API of a
running stack, the way the web app drives it. Every step is checked; the run stops at the first
failure and says which.

 1. health — the API's `/readyz`;
 2. sign in as a throwaway editor of the dev organization (`scripts/e2e_user.py`);
 3. research — a pasted note becomes an ingested source with facts;
 4. text → plan → previz — "Create a 30-second TikTok explaining why most people misunderstand AI
    agents" (fixture LLM), with measured timings and predicted behavior coverage;
 5. approve → build → final MP4 (downloaded) and the observed coverage (only `*_CONFIRMED` counts as
    delivered), the claim ledger;
 6. a natural-language edit ("make him more skeptical") proposed, applied, rebuilt;
 7. a reusable spec template saved from the version;
 8. captions translated to German, the translation approved;
 9. packaging for TikTok (labelled template packaging: the fixture LLM has no packaging answer);
10. an export attempt, **refused** because dev renders carry mock provenance (§32).

Mock engines only: the video is a placeholder face and a synthetic voice, and the numbers in the
report are measured on this host. Needs a running stack: `make dev` (Compose) or `make infra-up &&
make dev-native`. Writes `.data/demo/final.mp4` and `.data/demo/report.json`.

    uv run python scripts/demo.py [--api http://localhost:8000] [--timeout 900]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".data" / "demo"
sys.path.insert(0, str(Path(__file__).resolve().parent))

BRIEF = "Create a 30-second TikTok explaining why most people misunderstand AI agents."
NOTE = (
    "Our interviews: agents plan the steps, pick the tools and check their own work until the job is done. "
    "Most people expect a chatbot that answers once."
)


class DemoError(SystemExit):
    pass


class Demo:
    def __init__(self, api: httpx.AsyncClient, timeout_s: float) -> None:
        self.api = api
        self.timeout_s = timeout_s
        self.started = time.monotonic()
        self.report: dict[str, Any] = {"steps": []}

    def step(self, name: str, ok: bool, detail: str, **data: Any) -> None:
        elapsed = round(time.monotonic() - self.started, 1)
        print(f"  {'✓' if ok else '✗'} [{elapsed:>6.1f} s] {name}: {detail}")
        self.report["steps"].append({"step": name, "ok": ok, "detail": detail, "at_s": elapsed, **data})
        if not ok:
            raise DemoError(f"demo failed at: {name}")

    @staticmethod
    def key() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    async def wait_for(self, path: str, done: set[str], field: str = "state") -> dict[str, Any]:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            response = await self.api.get(path)
            if response.status_code == 200 and response.json().get(field) in done:
                return dict(response.json())
            await asyncio.sleep(1.0)
        raise DemoError(f"timed out waiting for {path} to reach {sorted(done)}")

    async def download(self, path: str, dest: Path) -> int:
        response = await self.api.get(path, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307):
            response = await self.api.get(response.headers["location"])
        elif "application/json" in response.headers.get("content-type", ""):
            response = await self.api.get(response.json()["url"])
        response.raise_for_status()
        dest.write_bytes(response.content)
        return len(response.content)


async def run(base: str, timeout_s: float) -> dict[str, Any]:
    from e2e_user import create_editor, load_env

    load_env()
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(base_url=base, timeout=60) as api:
        demo = Demo(api, timeout_s)
        ready = await api.get("/readyz")
        demo.step("health", ready.status_code == 200, f"/readyz {ready.json().get('checks')}")

        user = await create_editor()
        login = await api.post(
            "/v1/auth/login", json={"email": user["email"], "password": user["password"], "org_id": user["org_id"]}
        )
        demo.step("sign in", login.status_code == 200, f"{user['email']} ({login.json().get('role')})")
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        project = (await api.post("/v1/projects", json={"name": f"Demo {time.strftime('%H:%M:%S')}"})).json()["id"]

        # research: a user-provided note
        added = await api.post(
            f"/v1/projects/{project}/sources",
            json={"kind": "note", "title": "Our interviews", "text": NOTE},
            headers=demo.key(),
        )
        source = await demo.wait_for(f"/v1/sources/{added.json()['source_id']}", {"ingested", "failed"}, "status")
        demo.step(
            "research source",
            source["status"] == "ingested",
            f"{source['status']}, trust {source['trust']}, {source['fact_count']} facts",
        )

        # text → plan → previz
        created = await api.post(f"/v1/projects/{project}/videos", json={"input": BRIEF}, headers=demo.key())
        demo.step("plan requested", created.status_code == 202, f"POST /videos → {created.status_code}")
        ids = created.json()
        job = await demo.wait_for(f"/v1/jobs/{ids['job_id']}", {"succeeded", "failed"}, "status")
        demo.step("plan", job["status"] == "succeeded", f"planning job {job['status']}")
        previz = await demo.wait_for(f"/v1/versions/{ids['version_id']}/previz", {"previz_ready", "failed"})
        report = previz.get("plan_report") or {}
        demo.step(
            "previz",
            previz["state"] == "previz_ready" and not previz["blocking"],
            f"planner {previz['planner']}, timings {previz['timing_source']}, "
            f"estimated {report.get('estimated_duration_s')} s, blocking findings {len(previz['blocking'])}",
        )

        # approve → build → final render
        approved = await api.post(f"/v1/versions/{ids['version_id']}:approve", json={}, headers=demo.key())
        demo.step("approve", approved.status_code == 202, f":approve → {approved.status_code}")
        version = await demo.wait_for(
            f"/v1/versions/{ids['version_id']}", {"ready", "partial", "failed", "needs_review"}
        )
        demo.step("build", version["state"] == "ready", f"version {version['state']}")
        renders = (await api.get(f"/v1/versions/{ids['version_id']}/renders")).json()
        final = next(r for r in renders if r["status"] == "ready" and not r["is_proxy"])
        size = await demo.download(f"/v1/renders/{final['id']}/download", OUT / "final.mp4")
        demo.step("final render", size > 10_000, f"{size} bytes → .data/demo/final.mp4 ({final['provenance_mode']})")
        coverage = (await api.get(f"/v1/versions/{ids['version_id']}/coverage")).json()
        entries = coverage.get("entries", [])
        delivered = sum(1 for e in entries if str(e.get("outcome") or "").endswith("_CONFIRMED"))
        demo.step("coverage", bool(entries), f"{len(entries)} requested items, {delivered} confirmed delivered")
        claims = (await api.get(f"/v1/versions/{ids['version_id']}/claims")).json()
        demo.step("claim ledger", isinstance(claims, list), f"{len(claims)} claims checked")

        # a natural-language edit
        spec = (await api.get(f"/v1/versions/{ids['version_id']}")).json()["spec"]
        last_scene = spec["scenes"][-1]["key"]  # scoped like a selection in the Video Studio
        edit = await api.post(
            f"/v1/versions/{ids['version_id']}/edits",
            json={"instruction": "make him more skeptical", "selection": {"scene_keys": [last_scene]}},
            headers=demo.key(),
        )
        proposal = await demo.wait_for(f"/v1/edits/{edit.json()['edit_proposal_id']}", {"proposed", "failed"}, "status")
        impact = proposal.get("impact") or {}
        demo.step(
            "edit proposed",
            proposal["status"] == "proposed",
            f"operations {[op['op'] for op in proposal.get('ops') or []]}, "
            f"regenerates {len(impact.get('regenerate') or [])} nodes"
            + ("" if proposal["status"] == "proposed" else f" — {impact.get('issues') or proposal.get('error')}"),
        )
        applied = await api.post(f"/v1/edits/{proposal['id']}:apply", json={}, headers=demo.key())
        child_id = applied.json()["new_version_id"]
        child = await demo.wait_for(f"/v1/versions/{child_id}", {"ready", "partial", "failed", "needs_review"})
        demo.step("edit applied", child["state"] == "ready", f"new version {child['number']} {child['state']}")

        # a reusable template from the edited version
        template = await api.post(
            "/v1/spec-templates", json={"name": "Demo captions", "kind": "caption", "from_version_id": child_id}
        )
        demo.step("template saved", template.status_code == 201, f"version {template.json().get('version')}")

        # captions in German
        translated = await api.post(
            f"/v1/versions/{child_id}/captions:translate", json={"language": "de"}, headers=demo.key()
        )
        demo.step("translation requested", translated.status_code == 202, f"→ {translated.status_code}")
        derived = translated.json()["new_version_id"]
        built = await demo.wait_for(f"/v1/versions/{derived}", {"ready", "partial", "failed", "needs_review"})
        captions = [c for c in (await api.get(f"/v1/versions/{derived}/captions")).json() if c["language"] == "de"]
        reviewed = await api.post(f"/v1/captions/{captions[0]['id']}:approve", json={"note": "demo review"})
        demo.step(
            "captions translated",
            built["state"] == "ready" and reviewed.status_code == 200,
            f"{len(captions)} German files, review {reviewed.json().get('review_state')}",
        )

        # packaging and the export guard
        packaged = await api.post(f"/v1/versions/{derived}:package", json={}, headers=demo.key())
        await demo.wait_for(f"/v1/jobs/{packaged.json()['job_id']}", {"succeeded", "failed"}, "status")
        rows = (await api.get(f"/v1/versions/{derived}/packaging")).json()
        demo.step(
            "packaging",
            bool(rows),
            "; ".join(f"{p['platform']}: {p['generator']['kind']} ({len(p['issues'])} notes)" for p in rows),
        )
        packaging = rows[0]
        await api.post(f"/v1/packaging/{packaging['id']}:approve")
        platform = next(p for p in (await api.get("/v1/platforms")).json() if p["id"] == packaging["platform"])
        renders = (await api.get(f"/v1/versions/{derived}/renders")).json()
        final = next(r for r in renders if r["status"] == "ready" and not r["is_proxy"])
        refused = await api.post(
            f"/v1/renders/{final['id']}/exports",
            json={
                "platform": packaging["platform"],
                "packaging_id": packaging["id"],
                "disclosure_checklist": {c["key"]: True for c in platform["checklist"]},
            },
            headers=demo.key(),
        )
        demo.step(
            "export guard",
            refused.status_code == 409 and "mock provenance" in refused.json().get("detail", "").lower(),
            f"refused as expected ({refused.status_code}): {refused.json().get('detail')}",
        )
        demo.report.update(
            {
                "api": base,
                "video_id": ids["video_id"],
                "versions": {"planned": ids["version_id"], "edited": child_id, "translated": derived},
                "total_s": round(time.monotonic() - demo.started, 1),
                "mock": True,
            }
        )
        return demo.report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--api", default=os.environ.get("API_BASE_URL", "http://localhost:8000"))
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per wait")
    args = parser.parse_args()
    print(f"Creator Engine demo (mock mode) against {args.api}")
    try:
        report = asyncio.run(run(args.api, args.timeout))
    except DemoError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 1
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"done in {report['total_s']} s — .data/demo/final.mp4, .data/demo/report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
