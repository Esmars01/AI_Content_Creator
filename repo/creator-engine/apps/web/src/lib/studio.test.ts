import { describe, expect, it } from "vitest";

import {
  coverageSummary,
  editableDraft,
  fieldKind,
  floorPlan,
  groupPack,
  memoryActions,
  memoryGroups,
  parseJson,
  plateCells,
  scorecardRows,
  sectionKey,
} from "./studio";

describe("DNA editor", () => {
  it("edits the newest draft and maps tabs to sections", () => {
    const versions = [
      { id: "a", number: 1, status: "approved" },
      { id: "b", number: 2, status: "draft" },
      { id: "c", number: 3, status: "draft" },
    ];
    expect(editableDraft(versions)?.id).toBe("c");
    expect(editableDraft(versions.slice(0, 1))).toBeNull();
    expect(sectionKey({ gesture: {} }, ["gesture_posture", "gesture"])).toBe("gesture");
    expect(sectionKey({}, ["world", "worlds"])).toBe("world");
  });
  it("edits scalars inline and structures as JSON", () => {
    expect(fieldKind(0.4)).toBe("number");
    expect(fieldKind("x")).toBe("string");
    expect(fieldKind([1])).toBe("json");
    expect(parseJson("{").error).toBeTruthy();
    expect(parseJson('{"a":1}').value).toEqual({ a: 1 });
  });
});

describe("Creator Test scorecard", () => {
  it("shows measured, rated and not-measured metrics honestly", () => {
    const rows = scorecardRows({
      identity_similarity: { status: "measured", p50: 0.91, min: 0.8, n: 12, mock: true, method: "face.embed" },
      wpm: { status: "measured", value: 152.4 },
      accent: { status: "not_measured", reason: "a human rating" },
      human_rating: { status: "rated", value: 4 },
    });
    expect(rows.map((r) => r.key)).toEqual(["identity_similarity", "wpm", "accent", "human_rating"]);
    expect(rows[0]).toMatchObject({ value: "p50 0.910 (min 0.800, n=12)", mock: true, measured: true });
    expect(rows[2]).toMatchObject({ value: "not measured", note: "a human rating", measured: false });
    expect(rows[3]).toMatchObject({ value: "4 / 5" });
    expect(coverageSummary({ requested_vs_observed: { items: 10, confirmed: 3 } })).toBe(
      "3 of 10 requested items observed (confirmed)",
    );
    expect(coverageSummary(null)).toBe("no coverage report");
  });
});

describe("galleries", () => {
  it("groups the identity pack and the plates", () => {
    const pack = groupPack([
      { asset_id: "1", kind: "angle", label: "front", similarity: 0.9, decision: "pending" },
      { asset_id: "2", kind: "expression", label: "smile", similarity: 0.8, decision: "approved" },
    ]);
    expect(pack.angles.map((i) => i.label)).toEqual(["front"]);
    expect(pack.expressions.map((i) => i.label)).toEqual(["smile"]);
    const cells = plateCells(
      { cam_a: { day: { clear: ["x", "y"] } } },
      { cam_a: { day: { clear: "x" } }, cam_b: { night: { rain: "z" } } },
    );
    expect(cells).toEqual([
      { position: "cam_a", time: "day", weather: "clear", candidates: ["x", "y"], chosen: "x" },
      { position: "cam_b", time: "night", weather: "rain", candidates: [], chosen: "z" },
    ]);
  });
  it("draws the floor plan from normalized positions", () => {
    const items = floorPlan(
      {
        elements: [{ key: "el_desk", label: "Desk", position: [0.5, 0.25, 0.7] }],
        camera_positions: [{ key: "cam_front", position: [0.5, 0.9, 1.2], status: "permitted" }, { key: "cam_x" }],
      },
      200,
    );
    expect(items).toEqual([
      { key: "el_desk", label: "Desk", kind: "element", x: 100, y: 50 },
      { key: "cam_front", label: "cam_front", kind: "camera", x: 100, y: 180, status: "permitted" },
    ]);
  });
});

describe("memory", () => {
  it("puts conflicts and proposals first and offers only possible actions", () => {
    const active = { id: "1", status: "active", pinned: false, conflict_state: "none", category: "persona" };
    const proposed = { ...active, id: "2", status: "proposed" };
    const conflicted = { ...active, id: "3", pinned: true, conflict_state: "unresolved" };
    expect(memoryGroups([active, proposed, conflicted]).flatMap(([, items]) => items.map((i) => i.id))).toEqual([
      "3",
      "2",
      "1",
    ]);
    expect(memoryActions(proposed)).toEqual(["activate", "dismiss"]);
    expect(memoryActions(conflicted)).toEqual(["unpin", "forget", "resolve_conflict"]);
    expect(memoryActions({ ...active, status: "forgotten" })).toEqual([]);
  });
});
