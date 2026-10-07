import { describe, expect, it } from "vitest";

import { invalidationsFor, reconnectDelay } from "./events";
import { keys, refreshBefore } from "./queries";

const queryKeys = (type: string, data: Record<string, unknown>) => invalidationsFor(type, data).map((i) => i.queryKey);

describe("live events", () => {
  it("job updates refresh the job lists, the job and its target", () => {
    const done = queryKeys("job.updated", {
      job_id: "j2",
      status: "succeeded",
      target_type: "video",
      target_id: "vid",
    });
    expect(done).toContainEqual(keys.video("vid"));
    expect(done).toContainEqual(keys.videosAll());
  });

  it("a running build refreshes only the version row, a finished one everything under it", () => {
    // Regression (audit S5): every finished node sent job.updated, which refetched every query under
    // the version (renders, storyboard, takes, QC, …) dozens of times per build.
    const running = invalidationsFor("job.updated", {
      job_id: "j1",
      status: "running",
      target_type: "video_version",
      target_id: "v1",
    });
    expect(running).toEqual([
      { queryKey: keys.jobs() },
      { queryKey: keys.job("j1") },
      { queryKey: keys.version("v1"), exact: true },
    ]);
    const finished = invalidationsFor("job.updated", {
      job_id: "j1",
      status: "failed",
      target_type: "video_version",
      target_id: "v1",
    });
    expect(finished).toContainEqual({ queryKey: keys.version("v1") });
    expect(finished).toContainEqual({ queryKey: keys.videosAll() });
  });

  it("version, previz, render and coverage events refresh what they change", () => {
    expect(queryKeys("previz.ready", { version_id: "v1" })).toContainEqual(keys.storyboard("v1"));
    expect(queryKeys("version.updated", { version_id: "v1", state: "ready" })).toContainEqual(keys.version("v1"));
    expect(queryKeys("render.ready", { version_id: "v1" })).toEqual([keys.renders("v1")]);
    expect(queryKeys("coverage.updated", { version_id: "v1" })).toEqual([keys.coverage("v1")]);
  });

  it("QC, critiques and consistency live under the version, so its refresh reaches them", () => {
    for (const key of [keys.qc("v1"), keys.critiques("v1"), keys.versionConsistency("v1")]) {
      expect(key.slice(0, 2)).toEqual(keys.version("v1"));
    }
  });

  it("a gap refetches everything; unknown events nothing", () => {
    expect(queryKeys("stream.gap", {})).toEqual([[]]);
    expect(queryKeys("something.else", { version_id: "v1" })).toEqual([]);
    expect(queryKeys("version.updated", {})).toEqual([]);
  });

  it("a closed stream reconnects with a capped backoff", () => {
    expect([0, 1, 2, 3, 10].map(reconnectDelay)).toEqual([1000, 2000, 4000, 8000, 30_000]);
  });
});

describe("presigned URLs", () => {
  it("are refreshed a minute before they expire", () => {
    const now = Date.parse("2026-10-06T12:00:00Z");
    expect(refreshBefore("2026-10-06T12:15:00Z", now)).toBe(14 * 60_000);
    expect(refreshBefore("2026-10-06T12:00:30Z", now)).toBe(5_000); // never a tight loop
    expect(refreshBefore(null, now)).toBe(false);
    expect(refreshBefore("garbage", now)).toBe(false);
  });
});
