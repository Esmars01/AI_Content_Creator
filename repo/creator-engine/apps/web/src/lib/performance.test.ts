import { describe, expect, it } from "vitest";

import { buildLane, emotionColor, trajectorySentence } from "./performance";

const spec = {
  scenes: [
    {
      key: "scn_2",
      order: 1,
      acting: {
        states: [
          {
            key: "st_3",
            emotion: { displayed: { label: "serious" }, felt: { label: "serious" }, masking: false },
            transition_in: { style: "sudden", trigger: { kind: "realization" } },
          },
        ],
      },
    },
    {
      key: "scn_1",
      order: 0,
      acting: {
        states: [
          { key: "st_1", emotion: { displayed: { label: "calm" }, felt: { label: "surprised" }, masking: true } },
          { key: "st_2", emotion: { displayed: { label: "confident" }, felt: { label: "confident" } } },
        ],
      },
    },
  ],
} as never;

const timings = [
  { state_key: "st_1", start_s: 0, end_s: 2 },
  { state_key: "st_2", start_s: 2, end_s: 6 },
  { state_key: "st_3", start_s: 6, end_s: 10, drift_s: 0.25 },
];

describe("performance lane", () => {
  const lane = buildLane(spec, timings, [{ scene_key: "scn_1", event_key: "ev_1", type: "small_smile", at_s: 5 }], 10);

  it("places bands in time order with percentages of the duration", () => {
    expect(lane.bands.map((b) => b.key)).toEqual(["st_1", "st_2", "st_3"]);
    expect(lane.bands[1]).toMatchObject({ left: 20, width: 40, label: "confident" });
    expect(lane.bands[2]?.driftS).toBe(0.25);
  });

  it("shows masking and transitions", () => {
    expect(lane.bands[0]).toMatchObject({ masking: true, felt: "surprised" });
    expect(lane.transitions).toEqual([
      expect.objectContaining({ key: "st_3:in", atS: 6, label: "Sudden · Realization" }),
    ]);
  });

  it("places event markers", () => {
    expect(lane.events).toEqual([expect.objectContaining({ key: "ev_1", left: 50, label: "Small smile" })]);
  });

  it("says the trajectory in plain words", () => {
    expect(trajectorySentence(lane)).toBe("calm (hiding surprised) → confident → (realization) serious");
  });

  it("states without timings are left out and colors are stable", () => {
    expect(buildLane(spec, timings.slice(0, 1), [], 10).bands).toHaveLength(1);
    expect(emotionColor("ecstatic")).toBe(emotionColor("ecstatic"));
  });
});
