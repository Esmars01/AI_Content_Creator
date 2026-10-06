/**
 * Coverage badges (§31) under invariant I9 ("honest UI"): a behavior is shown as **delivered only
 * when its outcome is `*_CONFIRMED`**. Before observation a badge says what is *planned* (the
 * compiled level), never that something was delivered. All wording and tones live here so the
 * contract test (`coverage.contract.test.ts`) can check every outcome.
 */
import type { Schemas } from "./api";

export type CoverageEntry = Schemas["CoverageEntry"];
export type Outcome = Schemas["Outcome"];
export type CoverageLevel = Schemas["CoverageLevel"];

export type BadgeTone = "delivered" | "planned" | "approximate" | "missing" | "unsupported" | "unknown";

export interface Badge {
  label: string;
  tone: BadgeTone;
  /** true only for `*_CONFIRMED` outcomes (I9) */
  delivered: boolean;
  explanation: string;
}

export function isDelivered(outcome: string | null | undefined): boolean {
  return typeof outcome === "string" && outcome.endsWith("_CONFIRMED");
}

const OUTCOME_BADGES: Record<Outcome, Omit<Badge, "delivered">> = {
  HONORED_CONFIRMED: {
    label: "Delivered",
    tone: "delivered",
    explanation: "The engine controlled it and the analyzers saw it.",
  },
  APPROXIMATED_CONFIRMED: {
    label: "Delivered (approximated)",
    tone: "delivered",
    explanation: "Realized by an approximation (prompt, edit or audio) and seen in the result.",
  },
  HONORED_PARTIAL: {
    label: "Partly seen",
    tone: "approximate",
    explanation: "Controlled, but the analyzers saw it only partly.",
  },
  APPROXIMATED_PARTIAL: {
    label: "Partly seen",
    tone: "approximate",
    explanation: "Approximated, and seen only partly.",
  },
  HONORED_NOT_OBSERVED: {
    label: "Not seen",
    tone: "missing",
    explanation: "Requested and controlled, but not visible in the result.",
  },
  APPROXIMATED_NOT_OBSERVED: {
    label: "Not seen",
    tone: "missing",
    explanation: "Approximated, but not visible in the result.",
  },
  HONORED_CONTRADICTED: {
    label: "Contradicted",
    tone: "missing",
    explanation: "The result shows something else than what was requested.",
  },
  APPROXIMATED_CONTRADICTED: {
    label: "Contradicted",
    tone: "missing",
    explanation: "The approximation produced something else than what was requested.",
  },
  UNSUPPORTED: {
    label: "Not supported",
    tone: "unsupported",
    explanation: "The engine cannot direct this; nothing was promised.",
  },
  UNSUPPORTED_OBSERVED: {
    label: "Not supported (appeared anyway)",
    tone: "unsupported",
    explanation: "Not directable; something like it appeared on its own, which is not a delivery.",
  },
  NOT_MEASURABLE: {
    label: "Not measurable yet",
    tone: "unknown",
    explanation: "No analyzer can check this yet; it is not counted as delivered.",
  },
  NOT_APPLICABLE: {
    label: "Not applicable",
    tone: "unknown",
    explanation: "Nothing renders this item (for example, no shot shows it).",
  },
};

const LEVEL_BADGES: Record<CoverageLevel, Omit<Badge, "delivered">> = {
  HONORED: {
    label: "Planned: controlled",
    tone: "planned",
    explanation: "The routed engine declares direct control; not generated or checked yet.",
  },
  APPROXIMATED: {
    label: "Planned: approximated",
    tone: "approximate",
    explanation: "Will be approximated (prompt, edit or audio); not generated or checked yet.",
  },
  UNSUPPORTED: {
    label: "Unsupported",
    tone: "unsupported",
    explanation: "The routed engine cannot direct this.",
  },
};

export function outcomeBadge(outcome: Outcome): Badge {
  return { ...OUTCOME_BADGES[outcome], delivered: isDelivered(outcome) };
}

/** The badge for one coverage entry: its outcome after observation, its compiled level before. */
export function badgeFor(entry: Pick<CoverageEntry, "outcome" | "compiled">): Badge {
  if (entry.outcome) return outcomeBadge(entry.outcome);
  return { ...LEVEL_BADGES[entry.compiled.level], delivered: false };
}

export interface CoverageSummary {
  total: number;
  honored: number;
  approximated: number;
  unsupported: number;
  /** entries with an outcome (after observation) */
  observed: number;
  /** `*_CONFIRMED` outcomes only (I9) */
  delivered: number;
}

export function summarize(entries: readonly Pick<CoverageEntry, "outcome" | "compiled">[]): CoverageSummary {
  const out: CoverageSummary = { total: 0, honored: 0, approximated: 0, unsupported: 0, observed: 0, delivered: 0 };
  for (const entry of entries) {
    out.total += 1;
    if (entry.compiled.level === "HONORED") out.honored += 1;
    else if (entry.compiled.level === "APPROXIMATED") out.approximated += 1;
    else out.unsupported += 1;
    if (entry.outcome) {
      out.observed += 1;
      if (isDelivered(entry.outcome)) out.delivered += 1;
    }
  }
  return out;
}

/** The Simple-view badge text, e.g. "4 honored, 6 approximated, 2 unsupported". */
export function summaryText(summary: CoverageSummary): string {
  const planned = `${summary.honored} honored, ${summary.approximated} approximated, ${summary.unsupported} unsupported`;
  if (summary.observed === 0) return planned;
  return `${summary.delivered} of ${summary.total} delivered · ${planned}`;
}

/** The scene an entry belongs to: `/scenes[scn_1]/…` directly, segment items through the spec. */
export function sceneOf(itemRef: string, segmentScene: Readonly<Record<string, string>>): string | null {
  const scene = /^\/scenes\[([^\]]+)\]/.exec(itemRef);
  if (scene?.[1]) return scene[1];
  const segment = /^\/script\/segments\[([^\]]+)\]/.exec(itemRef);
  if (segment?.[1]) return segmentScene[segment[1]] ?? null;
  return null;
}

export function bySceneKey(
  entries: readonly CoverageEntry[],
  segmentScene: Readonly<Record<string, string>>,
): Record<string, CoverageEntry[]> {
  const out: Record<string, CoverageEntry[]> = {};
  for (const entry of entries) {
    const scene = sceneOf(entry.item_ref, segmentScene) ?? "video";
    (out[scene] ??= []).push(entry);
  }
  return out;
}

export const TONE_CLASSES: Record<BadgeTone, string> = {
  delivered: "border-green-700 bg-green-50 text-green-900",
  planned: "border-blue-700 bg-blue-50 text-blue-900",
  approximate: "border-amber-700 bg-amber-50 text-amber-900",
  missing: "border-red-700 bg-red-50 text-red-900",
  unsupported: "border-slate-500 bg-slate-100 text-slate-800",
  unknown: "border-slate-400 bg-white text-slate-700",
};
