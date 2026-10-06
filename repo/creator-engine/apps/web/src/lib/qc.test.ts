import { describe, expect, it } from "vitest";

import { consistencySeries, ladderSteps, mediaKind, metricRows, scoreBars, statusTone, triadRows } from "./qc";

describe("QC report", () => {
  it("renders the ladder history", () => {
    const steps = ladderSteps([
      { step: "attempt_seed", outcome: "run", take: 1, qc_retry: 1 },
      { step: "fallback_route", outcome: "skipped", reason: "no fallback route" },
      { step: "cheaper_fix", outcome: "proposed", reason: "proposed", proposal: { op: "lipsync_patch" } },
      { step: "needs_review", outcome: "flagged", reason: "best attempt kept" },
    ]);
    expect(steps.map((s) => s.tone)).toEqual(["info", "neutral", "warning", "danger"]);
    expect(steps[0]?.detail).toBe("take 1, attempt 1");
    expect(steps[2]?.detail).toContain("lipsync_patch");
    expect(ladderSteps(undefined)).toEqual([]);
  });

  it("keeps advisory and unjudged metrics apart from failures", () => {
    const rows = metricRows({
      "qc.vqa": { adapter_id: "mock_qc", score: 0.3, passed: false, advisory: false, thresholds: { min_score: 0.5 } },
      "qc.lipsync": { adapter_id: "syncnet_v1", score: 2, passed: false, advisory: true, thresholds: {} },
      "qc.speech_quality": { adapter_id: "utmos_v2", score: 3.1, passed: null, advisory: true },
    });
    expect(rows.map((r) => r.verdict)).toEqual(["advisory", "not judged", "fail"]);
    expect(rows[2]?.threshold).toBe("≥ score 0.5");
  });

  it("only confirmed outcomes count as delivered", () => {
    const rows = triadRows([
      {
        item_ref: "a",
        dimension: "gaze",
        outcome: "HONORED_CONFIRMED",
        compiled: { level: "HONORED", method: "native_parametric" },
      },
      { item_ref: "b", dimension: "gaze", outcome: "APPROXIMATED_NOT_OBSERVED", expected_for_method: true },
      { item_ref: "c", dimension: "smile", outcome: "HONORED_CONTRADICTED" },
      { item_ref: "d", dimension: "pitch", outcome: "NOT_MEASURABLE" },
    ]);
    expect(rows.map((r) => r.delivered)).toEqual([true, false, false, false]);
    expect(rows[1]?.outcome).toContain("expected: approximated editorially");
    expect(rows[2]?.tone).toBe("danger");
    expect(rows[3]?.tone).toBe("neutral");
  });
});

describe("critique and consistency", () => {
  it("never shows an unmeasured score as a number", () => {
    const bars = scoreBars({
      hook: { score: 0.8, basis: "first scene 2 s" },
      realism: { score: null, basis: "no VLM pass" },
      _meta: { critic: "rules-v1" },
    });
    expect(bars.map((b) => b.key)).toEqual(["hook", "realism"]);
    expect(bars[1]?.value).toBeNull();
  });

  it("charts consistency across videos oldest first", () => {
    const { dimensions, points } = consistencySeries([
      {
        version_id: "v2",
        created_at: "2026-10-05T02:00:00Z",
        verdict: "warn",
        metrics: { dimensions: { voice_identity: { status: "out_of_band" } } },
      },
      {
        version_id: "v1",
        created_at: "2026-10-05T01:00:00Z",
        verdict: "in_band",
        metrics: { dimensions: { face_identity: { status: "in_band" } } },
      },
    ]);
    expect(points.map((p) => p.versionId)).toEqual(["v1", "v2"]);
    expect(dimensions).toEqual(["face_identity", "voice_identity"]);
    expect(statusTone("out_of_band")).toBe("warning");
    expect(statusTone("not_measured")).toBe("neutral");
  });

  it("picks the player for a pair's media", () => {
    expect(mediaKind("video/mp4")).toBe("video");
    expect(mediaKind("audio/wav")).toBe("audio");
    expect(mediaKind("image/png")).toBe("image");
    expect(mediaKind("application/json")).toBe("other");
  });
});
