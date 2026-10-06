import { describe, expect, it } from "vitest";

import { invalidationsFor } from "./events";
import { keys } from "./queries";

describe("live events", () => {
  it("job updates refresh the job lists, the job and its target", () => {
    const out = invalidationsFor("job.updated", {
      job_id: "j1",
      status: "running",
      target_type: "video_version",
      target_id: "v1",
    });
    expect(out).toEqual([keys.jobs(), keys.job("j1"), keys.version("v1")]);
    const done = invalidationsFor("job.updated", {
      job_id: "j2",
      status: "succeeded",
      target_type: "video",
      target_id: "vid",
    });
    expect(done).toContainEqual(keys.video("vid"));
    expect(done).toContainEqual(keys.videosAll());
  });

  it("version, previz, render and coverage events refresh what they change", () => {
    expect(invalidationsFor("previz.ready", { version_id: "v1" })).toContainEqual(keys.storyboard("v1"));
    expect(invalidationsFor("version.updated", { version_id: "v1", state: "ready" })).toContainEqual(keys.previz("v1"));
    expect(invalidationsFor("render.ready", { version_id: "v1" })).toEqual([keys.renders("v1")]);
    expect(invalidationsFor("coverage.updated", { version_id: "v1" })).toEqual([keys.coverage("v1")]);
  });

  it("a gap refetches everything; unknown events nothing", () => {
    expect(invalidationsFor("stream.gap", {})).toEqual([[]]);
    expect(invalidationsFor("something.else", { version_id: "v1" })).toEqual([]);
    expect(invalidationsFor("version.updated", {})).toEqual([]);
  });
});
