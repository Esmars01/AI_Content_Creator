import { describe, expect, it } from "vitest";

import { EMPTY, STEPS, toRequest } from "./create-form";

describe("create form", () => {
  it("has the ten §31 steps", () => {
    expect(STEPS).toHaveLength(10);
    expect(STEPS.at(-1)).toBe("Plan");
  });

  it("leaves empty choices to the Director", () => {
    const body = toRequest({ ...EMPTY, input: "An idea" });
    expect(body).toMatchObject({
      input: "An idea",
      input_mode: "auto",
      cast: [],
      world_id: null,
      mode: null,
      primary_aspect: null,
      target_duration_s: null,
      style: { camera_profile_id: null, caption_style_id: null, music_mood: null },
      advanced: { takes: 1, intent_hints: [], acting_hints: [] },
    });
  });

  it("maps choices, the voice override and hints", () => {
    const body = toRequest({
      ...EMPTY,
      input: "Script",
      inputMode: "exact_script",
      creatorId: "c1",
      voiceVersionId: "vv1",
      duration: "45",
      aspect: "9:16",
      captionStyle: "bold_pop_highlight",
      actingHints: "deadpan\n\n then a crack of a smile \n",
      budget: "2.5",
    });
    expect(body.cast).toEqual([{ creator_id: "c1", role: "host", voice_version_id: "vv1" }]);
    expect(body.target_duration_s).toBe(45);
    expect(body.primary_aspect).toBe("9:16");
    expect(body.style?.caption_style_id).toBe("bold_pop_highlight");
    expect(body.advanced?.acting_hints).toEqual(["deadpan", "then a crack of a smile"]);
    expect(body.budget_usd).toBe(2.5);
  });
});
