import { describe, expect, it } from "vitest";

import {
  claimAction,
  claimSummary,
  embeddingStatus,
  exportBlockers,
  type ExportState,
  limitCheck,
  memoryProvenance,
  parseHashtags,
  sourceStatus,
  templateSlots,
} from "./phase12";

describe("memory provenance", () => {
  it("reads the source object", () => {
    const p = memoryProvenance({ type: "observation", mock: true, evidence: [{ video_id: "a" }, { video_id: "b" }] });
    expect(p).toEqual({ label: "Observed in generated videos", videoId: null, mock: true, videos: 2 });
    expect(memoryProvenance({ type: "plan", video_id: "v" }).videoId).toBe("v");
    expect(memoryProvenance("authored").label).toBe("Authored");
    expect(memoryProvenance(null).label).toBe("Unknown");
  });
  it("says whether an item is embedded", () => {
    expect(embeddingStatus(null).indexed).toBe(false);
    expect(embeddingStatus("mock_embed:mock-embed@1").label).toBe("Embedded (mock_embed)");
  });
});

describe("research", () => {
  it("explains failed sources", () => {
    expect(sourceStatus("failed", { code: "ssrf_blocked" }).label).toMatch(/private/);
    expect(sourceStatus("pending").tone).toBe("info");
    expect(sourceStatus("ingested").tone).toBe("success");
  });
  it("summarizes the ledger and offers only allowed actions", () => {
    const claims = [
      { verdict: "supported", blocking: false, overridable: true, closed_book: false },
      { verdict: "unsupported", blocking: true, overridable: true, closed_book: false },
      { verdict: "unsupported", blocking: true, overridable: false, closed_book: true },
      { verdict: "uncertain", blocking: true, overridable: true, closed_book: false, override_by: "u" },
      { verdict: "unsupported", blocking: true, overridable: true, closed_book: false, in_version: false },
    ];
    expect(claimSummary(claims)).toEqual({
      counts: { supported: 1, unsupported: 2, uncertain: 1 },
      blocking: 2,
      overridable: 1,
    });
    expect(claims.map(claimAction)).toEqual([null, "override", "closed_book", "overridden", "override"]);
  });
});

describe("packaging", () => {
  it("counts characters, not UTF-16 units", () => {
    expect(limitCheck("héllo 👋", 7)).toMatchObject({ length: 7, over: false });
    expect(limitCheck("x".repeat(101), 100).over).toBe(true);
  });
  it("parses hashtags", () => {
    expect(parseHashtags("#AI, ai  #Agents ＃tips")).toEqual(["AI", "Agents", "tips"]);
  });
});

describe("exports", () => {
  const render = { provenance_mode: "real", is_proxy: false, status: "ready", preset_id: "tiktok_1080x1920_30" };
  const ready: ExportState = {
    render,
    versionState: "ready",
    presets: ["tiktok_1080x1920_30"],
    packaging: { status: "approved", platform: "tiktok" },
    requireApprovedPackaging: true,
    checklist: [{ key: "ai_generated_label", label: "AI label on", required: true }],
    checked: { ai_generated_label: true },
    blockingClaims: 0,
    pendingTranslations: ["de"],
    chosenLanguages: null,
  };
  it("is clear when every rule passes", () => {
    expect(exportBlockers(ready)).toEqual([]);
  });
  it("mirrors each refusal of the API", () => {
    expect(exportBlockers({ ...ready, render: { ...render, provenance_mode: "mock_dev" } })[0]).toMatch(
      /mock provenance/,
    );
    expect(exportBlockers({ ...ready, versionState: "needs_review" })).toHaveLength(1);
    expect(exportBlockers({ ...ready, packaging: { status: "draft", platform: "tiktok" } })).toHaveLength(1);
    expect(exportBlockers({ ...ready, checked: {} })).toEqual(["Confirm: AI label on"]);
    expect(exportBlockers({ ...ready, blockingClaims: 2 })).toEqual(["2 unsupported claim(s) block the export."]);
    expect(exportBlockers({ ...ready, chosenLanguages: ["en", "de"] })).toEqual([
      "The de translation is not approved.",
    ]);
    expect(exportBlockers({ ...ready, presets: ["other"] })).toHaveLength(1);
  });
});

describe("templates", () => {
  it("lists slots", () => {
    expect(
      templateSlots({
        captions: { style_id: "minimal_lower" },
        render: { outputs: [{ preset_id: "tiktok_1080x1920_30", aspect: "9:16" }] },
        shot_defaults: { camera: { framing: "close_up" } },
      }),
    ).toEqual([
      { path: "/captions/style_id", value: "minimal_lower" },
      { path: "/render/outputs", value: '{"preset_id":"tiktok_1080x1920_30","aspect":"9:16"}' },
      { path: "/shot_defaults/camera/framing", value: "close_up" },
    ]);
  });
});
