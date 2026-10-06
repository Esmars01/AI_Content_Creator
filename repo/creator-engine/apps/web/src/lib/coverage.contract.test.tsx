/**
 * I9 contract test ("honest UI", §4): a behavior is shown as delivered only when its outcome is
 * `*_CONFIRMED`. The outcome list is read from the generated JSON Schema, so a new outcome in the
 * domain model fails here until the UI maps it. `tests/invariants/test_i09_honest_ui.py` runs this.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CoverageBadge, SceneCoverageBadge } from "@/components/coverage";

import {
  badgeFor,
  type CoverageEntry,
  isDelivered,
  type Outcome,
  outcomeBadge,
  summarize,
  summaryText,
} from "./coverage";

const schema = JSON.parse(
  readFileSync(
    resolve(__dirname, "../../../../packages/ts/api-client/schema/behavior_coverage_report.schema.json"),
    "utf8",
  ),
) as {
  $defs: { Outcome: { enum: Outcome[] }; CoverageLevel: { enum: ("HONORED" | "APPROXIMATED" | "UNSUPPORTED")[] } };
};
const OUTCOMES = schema.$defs.Outcome.enum;
const LEVELS = schema.$defs.CoverageLevel.enum;

function entry(level: (typeof LEVELS)[number], outcome: Outcome | null = null): CoverageEntry {
  return {
    item_ref: "/scenes[scn_1]/acting/states[st_1]/emotion",
    character_key: "char_alex",
    dimension: "emotion_visual",
    requested: "confident 0.6",
    compiled: { level, method: "native_parametric", detail: "" },
    observed: null,
    outcome,
    expected_for_method: true,
    summary: "",
  } as CoverageEntry;
}

describe("I9: delivered means *_CONFIRMED", () => {
  it("knows all 12 outcomes of the domain model", () => {
    expect(OUTCOMES).toHaveLength(12);
  });

  it.each(OUTCOMES)("%s", (outcome) => {
    const badge = outcomeBadge(outcome);
    expect(badge.delivered).toBe(outcome.endsWith("_CONFIRMED"));
    expect(isDelivered(outcome)).toBe(outcome.endsWith("_CONFIRMED"));
    expect(badge.tone === "delivered").toBe(outcome.endsWith("_CONFIRMED"));
    expect(/deliver/i.test(badge.label) && !badge.label.startsWith("Not")).toBe(outcome.endsWith("_CONFIRMED"));
    expect(badge.explanation.length).toBeGreaterThan(10);
  });

  it.each(LEVELS)("a %s item before observation is never delivered", (level) => {
    const badge = badgeFor(entry(level));
    expect(badge.delivered).toBe(false);
    expect(badge.label).not.toMatch(/deliver/i);
  });

  it("an unsupported item that appeared anyway is not a delivery", () => {
    expect(outcomeBadge("UNSUPPORTED_OBSERVED").delivered).toBe(false);
  });

  it("summaries count only confirmed outcomes as delivered", () => {
    const entries = [
      entry("HONORED", "HONORED_CONFIRMED"),
      entry("APPROXIMATED", "APPROXIMATED_CONFIRMED"),
      entry("HONORED", "HONORED_PARTIAL"),
      entry("UNSUPPORTED", "UNSUPPORTED_OBSERVED"),
      entry("HONORED", "NOT_MEASURABLE"),
    ];
    const summary = summarize(entries);
    expect(summary.delivered).toBe(2);
    expect(summaryText(summary)).toBe("2 of 5 delivered · 3 honored, 1 approximated, 1 unsupported");
    const predicted = summarize([entry("HONORED"), entry("APPROXIMATED")]);
    expect(predicted.delivered).toBe(0);
    expect(summaryText(predicted)).not.toMatch(/deliver/);
  });

  it("rendered badges carry the same truth", () => {
    for (const outcome of OUTCOMES) {
      const { unmount } = render(<CoverageBadge entry={entry("HONORED", outcome)} />);
      const shown = screen.getByText(outcomeBadge(outcome).label);
      expect(shown.getAttribute("data-delivered")).toBe(String(outcome.endsWith("_CONFIRMED")));
      unmount();
    }
    render(<SceneCoverageBadge entries={[entry("HONORED"), entry("UNSUPPORTED")]} />);
    expect(screen.getByTestId("scene-coverage").textContent).toBe("1 honored, 0 approximated, 1 unsupported");
    expect(screen.getByTestId("scene-coverage").getAttribute("data-delivered")).toBe("0");
  });
});
