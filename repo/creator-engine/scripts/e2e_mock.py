"""`make e2e-mock` (Phase 2–4 DoD) against the running Compose stack in mock mode.

1. The §11 fixture spec becomes a playable MP4 with captions and music on CPU in under 3 minutes,
   with a behavior coverage report holding requested, compiled and observed entries for every
   CBS item (and the matching viewer-level observation rows).
2. A second run of the same spec is 100% cache hits and yields the identical render.
3. Text to video through the HTTP API (Phase 4): `POST /v1/projects/{id}/videos` → the Director
   (fixture LLM) → previz with measured timings → `:approve` → a ready version.
4. Incremental editing over HTTP (Phase 6): the two-scene fixture is generated, then "make him
   more skeptical" (scoped to the reveal) is proposed and applied; only the reveal's behavior
   and performance re-run (the voice lock keeps the audio); compare shows the reveal's spec and
   CBS differences; restoring the original is a new version built entirely from cache hits.
5. Killing `worker-cpu` mid-task: its lease expires, the restarted worker finishes the build.

Requires `make dev` (infrastructure, API, orchestrator, scheduler, render worker, worker-cpu and
the dev seed). Writes `.data/e2e/final.mp4`, `.data/e2e/coverage.json` and `.data/e2e/report.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".data" / "e2e"
COMPOSE = [
    "docker",
    "compose",
    "-f",
    str(ROOT / "infra/compose/docker-compose.yml"),
    "--env-file",
    str(ROOT / ".env"),
    "--profile",
    "core",
    "--profile",
    "mock-gpu",
]
DOD_SECONDS = 180


def _load_env() -> None:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _check(condition: bool, message: str) -> None:
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        raise SystemExit(1)


async def _media(path: Path) -> dict[str, Any]:
    from ce_render.ffmpeg import measure_loudness, probe

    info = await probe(path)
    loudness = await measure_loudness(path)
    return {**info.as_dict(), "integrated_lufs": loudness.integrated_lufs, "true_peak_dbtp": loudness.true_peak_dbtp}


class _Queue:
    """Reads the scheduler's task table (the script runs on the host next to the Compose stack)."""

    def __init__(self) -> None:
        from ce_db.session import Database

        self.db = Database(os.environ["DATABASE_URL"], pool_size=1)

    async def _rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        import sqlalchemy as sa

        async with self.db.session() as session:
            return [dict(r) for r in (await session.execute(sa.text(sql), params)).mappings()]

    async def leased(self, version_id: str, capability: str) -> dict[str, Any] | None:
        rows = await self._rows(
            "select t.id, t.node_id, t.lease_worker_id from gpu_tasks t join execution_nodes n on n.id = t.node_id "
            "where n.version_id = :v and t.capability = :c and t.state in ('leased', 'running') limit 1",
            v=version_id,
            c=capability,
        )
        return rows[0] if rows else None

    async def held_by(self, task_id: Any, worker_id: Any) -> bool:
        rows = await self._rows(
            "select 1 from gpu_tasks where id = :t and lease_worker_id = :w and state in ('leased', 'running')",
            t=task_id,
            w=worker_id,
        )
        return bool(rows)

    async def attempts(self, node_id: Any) -> list[dict[str, Any]]:
        return await self._rows(
            "select reason, status, error_class, seed from job_attempts where node_id = :n order by attempt_no",
            n=node_id,
        )

    async def close(self) -> None:
        await self.db.dispose()


async def _coverage(version_id: str) -> dict[str, Any]:
    """The version's behavior coverage report and CBS documents (from the BuildManifest), and its
    viewer-level observation rows."""
    from ce_config.settings import load_effective
    from ce_storage import create_storage
    from ce_storage.content import ContentStore

    queue = _Queue()
    settings = load_effective(ROOT / "config").settings
    store = ContentStore(create_storage(settings), settings.s3_bucket_artifacts)
    try:
        docs = await queue._rows(
            "select m.node_key, a.sha256 from build_manifest_entries m join artifacts a "
            "on a.org_id = m.org_id and a.id = m.artifact_id where m.version_id = :v "
            "and (m.node_key like 'behavior.coverage:%' or m.node_key like 'behavior.resolve:%')",
            v=version_id,
        )
        data = {d["node_key"]: json.loads(await store.read_bytes(d["sha256"]))["data"] for d in docs}
        viewer = await queue._rows(
            "select item_ref, dimension, verdict, outcome from behavior_observations "
            "where version_id = :v and level_scope = 'viewer'",
            v=version_id,
        )
        version = await queue._rows("select coverage_summary from video_versions where id = :v", v=version_id)
    finally:
        await queue.close()
    coverage = data.get("behavior.coverage:video") or {}
    requested = [
        (c["item_ref"], c["dimension"])
        for key, doc in data.items()
        if key.startswith("behavior.resolve:")
        for c in doc["content"]["requested_controls"]
    ]
    return {
        "entries": (coverage.get("report") or {}).get("entries", []),
        "summary": coverage.get("summary", {}),
        "decision": coverage.get("decision"),
        "requested": requested,
        "viewer_rows": viewer,
        "coverage_summary": version[0]["coverage_summary"] if version else {},
    }


async def _kill_mid_task(client: Any, generate: Any) -> dict[str, Any]:
    """Starts a fresh-cache build, waits until `worker-cpu` holds an avatar render (the slowest mock
    task), kills the container (SIGKILL: the worker cannot report) and restarts it. A kill that
    lands after the task already finished proves nothing, so that case is retried."""
    queue = _Queue()
    try:
        for attempt in range(1, 4):
            build = await generate(
                ROOT / "config", wait=False, out=None, seed_namespace=str(uuid.uuid4()), timeout_s=60
            )
            task = None
            for _ in range(2400):
                task = await queue.leased(build["version_id"], "avatar.a2v")
                if task is not None:
                    break
                await asyncio.sleep(0.05)
            if task is None:
                raise SystemExit("  FAIL worker-cpu never leased an avatar task")
            subprocess.run([*COMPOSE, "kill", "worker-cpu"], check=True, capture_output=True)
            landed = await queue.held_by(task["id"], task["lease_worker_id"])
            subprocess.run([*COMPOSE, "up", "-d", "worker-cpu"], check=True, capture_output=True)
            started = time.monotonic()
            result = await asyncio.wait_for(client.get_workflow_handle(build["workflow_id"]).result(), 600)
            if not landed:
                print(f"       kill {attempt} landed after the task finished; retrying with a fresh build")
                continue
            print("       worker-cpu killed while holding an avatar task, then restarted")
            return {
                "state": result.get("state"),
                "seconds_after_kill": round(time.monotonic() - started, 1),
                "attempts": await queue.attempts(task["node_id"]),
                "kills": attempt,
            }
        raise SystemExit("  FAIL three kills in a row landed after the task finished")
    finally:
        await queue.close()


AI_AGENTS = "Create a 30-second TikTok explaining why most people misunderstand AI agents."


async def _e2e_editor() -> tuple[str, str]:
    """A throwaway editor in the dev org (the script runs next to the stack and owns its database)."""
    from e2e_user import create_editor  # scripts/e2e_user.py, next to this script

    user = await create_editor()
    return user["email"], user["password"]


async def _text_to_video(timeout_s: float) -> dict[str, Any]:
    """Phase 4 over HTTP: plan → previz → approve → ready, polling the API like a client would."""
    import httpx
    from ce_testing.fixtures import ALEX

    email, password = await _e2e_editor()
    base = os.environ.get("API_BASE_URL", "http://localhost:8000")
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base, timeout=60) as api:
        login = await api.post(
            "/v1/auth/login", json={"email": email, "password": password, "org_id": str(ALEX.ORG_ID)}
        )
        login.raise_for_status()
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        project = (await api.post("/v1/projects", json={"name": "e2e text to video"})).json()["id"]
        created = await api.post(
            f"/v1/projects/{project}/videos", json={"input": AI_AGENTS}, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        _check(created.status_code == 202, f"POST /videos → 202 ({created.status_code})")
        ids = created.json()

        async def wait_for(path: str, done: set[str], field: str = "state") -> dict[str, Any]:
            while time.monotonic() - started < timeout_s:
                response = await api.get(path)
                if response.status_code == 200 and response.json().get(field) in done:
                    return dict(response.json())
                await asyncio.sleep(1.0)
            raise SystemExit(f"  FAIL timed out waiting for {path} to reach {sorted(done)}")

        plan_job = await wait_for(f"/v1/jobs/{ids['job_id']}", {"succeeded", "failed"}, "status")
        _check(plan_job["status"] == "succeeded", f"plan job {plan_job['status']} {plan_job.get('error') or ''}")
        previz = await wait_for(f"/v1/versions/{ids['version_id']}/previz", {"previz_ready", "failed"})
        planned_s = round(time.monotonic() - started, 1)
        _check(previz["state"] == "previz_ready", f"previz ready in {planned_s} s ({previz['state']})")
        _check(previz["timing_source"] == "measured", "previz timings are measured (TTS + alignment)")
        _check(previz["blocking"] == [], "no blocking findings")
        approved = await api.post(
            f"/v1/versions/{ids['version_id']}:approve", json={}, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        _check(approved.status_code == 202, f":approve → 202 ({approved.status_code})")
        version = await wait_for(f"/v1/versions/{ids['version_id']}", {"ready", "partial", "failed"})
        _check(version["state"] == "ready", f"approved version {version['state']}")
        renders = (await api.get(f"/v1/versions/{ids['version_id']}/renders")).json()
        _check(any(r["status"] == "ready" and not r["is_proxy"] for r in renders), "final render ready")
    return {
        **ids,
        "planner": previz["planner"],
        "planned_and_previz_s": planned_s,
        "total_s": round(time.monotonic() - started, 1),
        "estimated_duration_s": previz["plan_report"]["estimated_duration_s"],
    }


async def _node_statuses(version_id: str) -> dict[str, str]:
    queue = _Queue()
    try:
        rows = await queue._rows(
            "select n.node_key, n.status from execution_nodes n join generation_jobs j on j.id = n.job_id "
            "where n.version_id = :v and j.kind = 'generate'",
            v=version_id,
        )
    finally:
        await queue.close()
    return {r["node_key"]: r["status"] for r in rows}


async def _edit_and_compare(timeout_s: float) -> dict[str, Any]:
    """Phase 6 over HTTP: propose → apply → ready; compare; restore (all cache hits)."""
    import httpx
    from ce_api.video_cli import generate_fixture
    from ce_testing.fixtures import ALEX

    parent = await generate_fixture(
        ROOT / "config", wait=True, out=None, seed_namespace=str(uuid.uuid4()), timeout_s=timeout_s, fixture="two_scene"
    )
    _check(parent.get("state") == "ready", f"two-scene fixture ready ({parent.get('state')})")
    email, password = await _e2e_editor()
    base = os.environ.get("API_BASE_URL", "http://localhost:8000")
    started = time.monotonic()
    key = {"Idempotency-Key": ""}
    async with httpx.AsyncClient(base_url=base, timeout=60) as api:
        login = await api.post(
            "/v1/auth/login", json={"email": email, "password": password, "org_id": str(ALEX.ORG_ID)}
        )
        login.raise_for_status()
        api.headers["X-CSRF-Token"] = login.json()["csrf_token"]

        async def wait_for(path: str, done: set[str], field: str = "state") -> dict[str, Any]:
            while time.monotonic() - started < timeout_s:
                response = await api.get(path)
                if response.status_code == 200 and response.json().get(field) in done:
                    return dict(response.json())
                await asyncio.sleep(1.0)
            raise SystemExit(f"  FAIL timed out waiting for {path} to reach {sorted(done)}")

        def fresh() -> dict[str, str]:
            key["Idempotency-Key"] = str(uuid.uuid4())
            return dict(key)

        created = await api.post(
            f"/v1/versions/{parent['version_id']}/edits",
            json={"instruction": "make him more skeptical", "selection": {"scene_keys": ["scn_reveal"]}},
            headers=fresh(),
        )
        _check(created.status_code == 202, f"POST /edits → 202 ({created.status_code})")
        proposal = await wait_for(f"/v1/edits/{created.json()['edit_proposal_id']}", {"proposed", "failed"}, "status")
        _check(proposal["status"] == "proposed", f"proposal {proposal['status']} {proposal['impact'].get('issues')}")
        _check(
            [op["op"] for op in proposal["ops"]] == ["set_acting", "add_behavior_event"],
            "operations: set_acting + add_behavior_event",
        )
        runs = {*proposal["impact"]["regenerate"], *proposal["impact"]["cascade"]}
        _check(not any(k.startswith(("tts.", "asr.", "align.")) for k in runs), "the voice lock keeps the audio")
        applied = await api.post(f"/v1/edits/{proposal['id']}:apply", json={}, headers=fresh())
        _check(applied.status_code == 202, f":apply → 202 ({applied.status_code})")
        child_id = applied.json()["new_version_id"]
        child = await wait_for(f"/v1/versions/{child_id}", {"ready", "partial", "failed"})
        _check(child["state"] == "ready" and child["origin"] == "edit", f"derived version {child['state']}")
        statuses = await _node_statuses(child_id)
        ran = {k for k, v in statuses.items() if v == "succeeded"}
        _check("avatar.render:sht_4:c1:t1" in ran, "the reveal is re-performed")
        hook = [k for k in statuses if k.endswith((":scn_hook", ":sht_1")) or ":sht_1:" in k]
        _check(bool(hook) and all(statuses[k] == "cached" for k in hook), f"the hook is all cache hits ({len(hook)})")
        compare = (
            await api.get(f"/v1/videos/{parent['video_id']}/compare", params={"a": parent["version_id"], "b": child_id})
        ).json()
        _check(
            bool(compare["spec"]) and {d["area"] for d in compare["spec"]} <= {"acting", "intent"},
            f"compare: {len(compare['spec'])} spec differences in acting/intent",
        )
        _check(set(compare["cbs"]) == {"scn_reveal"}, f"compare: CBS differs in {sorted(compare['cbs'])}")
        restored = await api.post(f"/v1/versions/{parent['version_id']}:restore", headers=fresh())
        _check(restored.status_code == 202, f":restore → 202 ({restored.status_code})")
        again = await wait_for(f"/v1/versions/{restored.json()['version_id']}", {"ready", "partial", "failed"})
        restored_statuses = await _node_statuses(again["id"])
        _check(
            again["state"] == "ready" and set(restored_statuses.values()) == {"cached"},
            f"restore: a new version (v{again['number']}) built entirely from cache hits",
        )
    return {
        "parent": parent["version_id"],
        "child": child_id,
        "ran": sorted(ran),
        "restored": again["id"],
        "seconds": round(time.monotonic() - started, 1),
    }


async def main(kill: bool) -> int:
    _load_env()
    from ce_api.video_cli import generate_fixture
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    OUT.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {}
    namespace = str(uuid.uuid4())  # a fresh cache for this run

    print("1/5 fixture spec → MP4 (cold cache)")
    first = await generate_fixture(
        ROOT / "config", wait=True, out=OUT / "final.mp4", seed_namespace=namespace, timeout_s=DOD_SECONDS + 60
    )
    report["first"] = first
    _check(first.get("state") == "ready", f"version ready ({first.get('state')}, failed={first.get('failed')})")
    _check(first["seconds"] < DOD_SECONDS, f"built in {first['seconds']} s (< {DOD_SECONDS} s on CPU)")
    media = await _media(OUT / "final.mp4")
    report["media"] = media
    _check((media["width"], media["height"]) == (1080, 1920), f"1080x1920 ({media['width']}x{media['height']})")
    _check(media.get("video_codec") == "h264" and media.get("audio_codec") == "aac", "H.264 video with AAC audio")
    _check(3.0 < media["duration_s"] < 15.0, f"duration {media['duration_s']} s")
    _check(abs(media["integrated_lufs"] + 14.0) <= 1.5, f"loudness {media['integrated_lufs']} LUFS (target -14)")

    coverage = await _coverage(first["version_id"])
    entries = coverage["entries"]
    by_item = {(e["item_ref"], e["dimension"]): e for e in entries}
    requested = coverage["requested"]
    (OUT / "coverage.json").write_text(json.dumps(coverage, indent=2, default=str), encoding="utf-8")
    report["coverage"] = {"items": len(requested), "summary": coverage["summary"], "decision": coverage["decision"]}
    _check(
        bool(requested) and set(by_item) == set(requested),
        f"a coverage entry for each of the {len(requested)} CBS items",
    )
    complete = [
        e
        for e in entries
        if e.get("requested") and (e.get("compiled") or {}).get("level") and (e.get("compiled") or {}).get("method")
        and (e.get("observed") or {}).get("verdict") and e.get("outcome")
    ]  # fmt: skip
    _check(len(complete) == len(requested), f"requested, compiled and observed in all {len(complete)} entries")
    rows = {(r["item_ref"], r["dimension"]) for r in coverage["viewer_rows"]}
    _check(rows == set(requested), f"{len(rows)} viewer-level behavior_observations rows")
    _check(coverage["coverage_summary"].get("items") == len(requested), "the version's coverage summary is recorded")

    print("2/5 same spec again (warm cache)")
    second = await generate_fixture(
        ROOT / "config", wait=True, out=None, seed_namespace=namespace, timeout_s=DOD_SECONDS
    )
    report["second"] = second
    _check(second.get("state") == "ready", "second version ready")
    _check(second["cached"] == second["nodes"], f"{second['cached']}/{second['nodes']} nodes were cache hits")
    _check(second.get("render_sha256") == first.get("render_sha256"), "identical render")

    print("3/5 text → plan → previz → approve → video (HTTP API)")
    report["text_to_video"] = await _text_to_video(DOD_SECONDS * 3)

    print("4/5 edit: make him more skeptical → apply → compare → restore (HTTP API)")
    report["edit"] = await _edit_and_compare(DOD_SECONDS * 3)

    if kill:
        print("5/5 kill worker-cpu mid-task")
        client = await Client.connect(
            os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
        )
        killed = await _kill_mid_task(client, generate_fixture)
        report["kill"] = killed
        _check(killed["state"] == "ready", f"build finished {killed['seconds_after_kill']} s after the kill")
        attempts = killed["attempts"]
        _check(any(a["error_class"] == "lease_expired" for a in attempts), "the killed task's lease expired")
        expired = next(a for a in attempts if a["error_class"] == "lease_expired")
        retried = [a for a in attempts if a["reason"] == "infra_retry" and a["status"] == "succeeded"]
        _check(bool(retried) and retried[0]["seed"] == expired["seed"], "its infra retry succeeded with the same seed")
    (OUT / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"e2e-mock passed; render at {OUT / 'final.mp4'}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-kill", action="store_true", help="skip the worker-kill check")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(kill=not args.no_kill)))
