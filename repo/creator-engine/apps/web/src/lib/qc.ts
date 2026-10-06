/** QC report, critique, consistency and benchmark display logic (§26, §20, §24; Phase 11). Pure
 * functions; the panels render them. Nothing here upgrades an unmeasured value into a pass (§16.8). */

export type Json = Record<string, unknown>;

export interface LadderStep {
  step: string;
  outcome: string;
  label: string;
  detail: string;
  tone: "success" | "warning" | "danger" | "info" | "neutral";
}

const STEP_LABEL: Record<string, string> = {
  attempt_seed: "Retry with a new seed",
  fallback_route: "Retry on the fallback engine",
  cheaper_fix: "Cheaper fix",
  needs_review: "Flagged for review",
  pass: "Passed",
};

/** The ladder history of a shot gate as display rows (run, skipped, proposed, flagged, passed). */
export function ladderSteps(ladder: Json[] | undefined): LadderStep[] {
  return (ladder ?? []).map((h) => {
    const step = String(h.step ?? "");
    const outcome = String(h.outcome ?? "");
    const label = STEP_LABEL[step] ?? step;
    let detail = String(h.reason ?? "");
    if (outcome === "run") {
      const adapter = h.adapter_id ? ` on ${String(h.adapter_id)}` : "";
      detail = `take ${String(h.take ?? "?")}, attempt ${String(h.qc_retry ?? "?")}${adapter}`;
    }
    if (outcome === "proposed") {
      const op = (h.proposal as Json | undefined)?.op;
      detail = `${detail}${op ? ` (${String(op)})` : ""}`;
    }
    const tone: LadderStep["tone"] =
      outcome === "passed"
        ? "success"
        : outcome === "flagged" || outcome === "failed"
          ? "danger"
          : outcome === "skipped"
            ? "neutral"
            : outcome === "proposed"
              ? "warning"
              : "info";
    return { step, outcome, label, detail, tone };
  });
}

export interface MetricRow {
  capability: string;
  adapter: string;
  score: string;
  threshold: string;
  verdict: "pass" | "fail" | "advisory" | "not judged";
  reason: string;
}

/** A take's metric verdicts (thresholds keyed by the adapter that measured them, §26). */
export function metricRows(verdicts: Json | undefined): MetricRow[] {
  return Object.entries(verdicts ?? {})
    .map(([capability, raw]) => {
      const v = (raw ?? {}) as Json;
      const passed = v.passed as boolean | null | undefined;
      const advisory = Boolean(v.advisory);
      const thresholds = (v.thresholds ?? {}) as Record<string, number>;
      const verdict: MetricRow["verdict"] =
        passed === null || passed === undefined ? "not judged" : passed ? "pass" : advisory ? "advisory" : "fail";
      return {
        capability,
        adapter: String(v.adapter_id ?? "?"),
        score: typeof v.score === "number" ? v.score.toFixed(2) : "—",
        threshold:
          Object.entries(thresholds)
            .map(([k, n]) => `${k.replace("min_", "≥ ").replace("max_", "≤ ").replace("_", " ")} ${n}`)
            .join(", ") || "—",
        verdict,
        reason: String(v.reason ?? ""),
      };
    })
    .sort((a, b) => a.capability.localeCompare(b.capability));
}

export interface TriadRow {
  item: string;
  dimension: string;
  requested: string;
  compiled: string;
  observed: string;
  outcome: string;
  delivered: boolean;
  tone: "success" | "warning" | "danger" | "neutral";
}

/** Requested → compiled → observed per CBS item (§16.4): only `*_CONFIRMED` counts as delivered. */
export function triadRows(entries: Json[] | undefined): TriadRow[] {
  return (entries ?? []).map((e) => {
    const compiled = (e.compiled ?? {}) as Json;
    const observed = (e.observed ?? null) as Json | null;
    const outcome = String(e.outcome ?? "");
    const delivered = outcome.endsWith("_CONFIRMED");
    const failed = outcome.endsWith("_NOT_OBSERVED") || outcome.endsWith("_CONTRADICTED");
    const expected = Boolean(e.expected_for_method);
    return {
      item: String(e.item_ref ?? ""),
      dimension: String(e.dimension ?? ""),
      requested: String(e.requested ?? ""),
      compiled: `${String(compiled.level ?? "?")} · ${String(compiled.method ?? "?")}`,
      observed: observed ? String(observed.verdict ?? "—") : "—",
      outcome: failed
        ? `FAILED OBSERVATION${expected ? " (expected: approximated editorially, not visible on the face)" : ""}`
        : outcome.endsWith("_PARTIAL")
          ? "PARTIAL"
          : outcome || "—",
      delivered,
      tone: delivered
        ? "success"
        : failed && !expected
          ? "danger"
          : outcome.endsWith("_PARTIAL")
            ? "warning"
            : "neutral",
    };
  });
}

export interface ScoreBar {
  key: string;
  label: string;
  value: number | null;
  basis: string;
}

/** Critique scores (0..1 or not measured), in the order of §26. */
export function scoreBars(scores: Json | undefined): ScoreBar[] {
  return Object.entries(scores ?? {})
    .filter(([k]) => !k.startsWith("_"))
    .map(([key, raw]) => {
      const s = (raw ?? {}) as Json;
      return {
        key,
        label: key.replace(/_/g, " "),
        value: typeof s.score === "number" ? s.score : null,
        basis: String(s.basis ?? ""),
      };
    });
}

export interface ConsistencyPoint {
  versionId: string;
  createdAt: string;
  verdict: string;
  status: Record<string, string>;
}

/** Consistency reports across a creator's videos (oldest first) for the Consistency tab (§20). */
export function consistencySeries(reports: Json[] | undefined): {
  dimensions: string[];
  points: ConsistencyPoint[];
} {
  const points = (reports ?? [])
    .map((r) => {
      const dims = ((r.metrics as Json | undefined)?.dimensions ?? {}) as Record<string, Json>;
      return {
        versionId: String(r.version_id ?? ""),
        createdAt: String(r.created_at ?? ""),
        verdict: String(r.verdict ?? ""),
        status: Object.fromEntries(Object.entries(dims).map(([k, v]) => [k, String(v.status ?? "not_measured")])),
      };
    })
    .sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const dimensions = [...new Set(points.flatMap((p) => Object.keys(p.status)))].sort();
  return { dimensions, points };
}

/** Badge tone of a consistency status. */
export function statusTone(status: string): "success" | "warning" | "danger" | "neutral" | "info" {
  if (status === "in_band") return "success";
  if (status === "out_of_band") return "warning";
  if (status === "deviation") return "info";
  return "neutral";
}

/** Whether a media type is shown as video, audio or image in the pairwise rating view. */
export function mediaKind(mime: string): "video" | "audio" | "image" | "other" {
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("image/")) return "image";
  return "other";
}
