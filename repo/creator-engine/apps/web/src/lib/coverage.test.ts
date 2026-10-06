import { describe, expect, it } from "vitest";

import { bySceneKey, type CoverageEntry, sceneOf } from "./coverage";

describe("scene grouping", () => {
  const segments = { seg_1: "scn_1", seg_2: "scn_2" };
  it("maps scene and segment item refs to scenes", () => {
    expect(sceneOf("/scenes[scn_3]/acting/events[ev_1]", segments)).toBe("scn_3");
    expect(sceneOf("/script/segments[seg_2]/annotations[an_1]", segments)).toBe("scn_2");
    expect(sceneOf("/audio/music", segments)).toBeNull();
  });
  it("groups entries, unknown ones under `video`", () => {
    const e = (ref: string) => ({ item_ref: ref, compiled: { level: "HONORED" } }) as unknown as CoverageEntry;
    const grouped = bySceneKey([e("/scenes[scn_1]/x"), e("/script/segments[seg_1]/y"), e("/other")], segments);
    expect(Object.keys(grouped).sort()).toEqual(["scn_1", "video"]);
    expect(grouped.scn_1).toHaveLength(2);
  });
});
