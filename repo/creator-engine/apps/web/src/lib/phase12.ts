/**
 * Display logic for the Phase 12 screens (memory provenance, research, claims, templates,
 * packaging, exports). Pure functions: the API decides every rule; these only explain its state
 * (no business rules in the frontend, §31).
 */
import { humanize } from "./format";

// ---------------------------------------------------------------------- Creator Memory provenance
export interface MemorySource {
  type?: string;
  video_id?: string;
  version_id?: string;
  edit_proposal_id?: string;
  mock?: boolean;
  evidence?: { video_id?: string }[];
  [key: string]: unknown;
}

export interface Provenance {
  label: string;
  videoId: string | null;
  mock: boolean;
  videos: number;
}

/** Where a memory item came from, in words (`source` is a JSON object, §18.4). */
export function memoryProvenance(source: MemorySource | string | null | undefined): Provenance {
  if (!source || typeof source === "string") {
    return { label: source ? humanize(source) : "Unknown", videoId: null, mock: false, videos: 0 };
  }
  const labels: Record<string, string> = {
    plan: "From an approved script",
    observation: "Observed in generated videos",
    user_edit: "From repeated edits",
    authored: "Authored",
    dna: "From Creator DNA",
    critique: "From a critique",
  };
  const videos = new Set((source.evidence ?? []).map((e) => e.video_id).filter(Boolean));
  if (source.video_id) videos.add(source.video_id);
  return {
    label: labels[source.type ?? ""] ?? humanize(source.type ?? "unknown"),
    videoId: source.video_id ?? null,
    mock: Boolean(source.mock),
    videos: videos.size,
  };
}

/** "embedded with <adapter>" or why not (an item is retrieved by keywords until it is). */
export function embeddingStatus(model: string | null | undefined): { indexed: boolean; label: string } {
  if (!model) return { indexed: false, label: "Not embedded yet (keyword retrieval)" };
  const adapter = model.split(":")[0] ?? model;
  return { indexed: true, label: `Embedded (${adapter})` };
}

// ---------------------------------------------------------------------- research and the claim ledger
export function sourceStatus(
  status: string,
  error?: { code?: string; message?: string } | null,
): {
  tone: "success" | "warning" | "danger" | "info";
  label: string;
} {
  if (status === "ingested") return { tone: "success", label: "Ingested" };
  if (status === "failed") {
    const reasons: Record<string, string> = {
      ssrf_blocked: "Blocked: the address is private or not allowed",
      fetch_failed: "Could not be fetched",
      extract_failed: "No readable text",
      unreadable: "Could not be read",
    };
    return { tone: "danger", label: reasons[error?.code ?? ""] ?? "Failed" };
  }
  return { tone: "info", label: "Ingesting…" };
}

export interface LedgerClaim {
  verdict: string;
  blocking: boolean;
  overridable: boolean;
  closed_book: boolean;
  override_by?: string | null;
  in_version?: boolean;
}

/** Counts per verdict and the claims that still block approval and export. */
export function claimSummary(claims: readonly LedgerClaim[]): {
  counts: Record<string, number>;
  blocking: number;
  overridable: number;
} {
  const counts: Record<string, number> = {};
  let blocking = 0;
  let overridable = 0;
  for (const claim of claims) {
    if (claim.in_version === false) continue;
    counts[claim.verdict] = (counts[claim.verdict] ?? 0) + 1;
    if (claim.blocking && !claim.override_by) {
      blocking += 1;
      if (claim.overridable && !claim.closed_book) overridable += 1;
    }
  }
  return { counts, blocking, overridable };
}

/** Which action the ledger offers for a claim (the API refuses the rest). */
export function claimAction(claim: LedgerClaim): "override" | "closed_book" | "overridden" | null {
  if (claim.override_by) return "overridden";
  if (claim.verdict === "supported") return null;
  if (claim.closed_book || !claim.overridable) return "closed_book";
  return "override";
}

// ---------------------------------------------------------------------- packaging limits
export interface LimitCheck {
  length: number;
  max: number;
  over: boolean;
  source: string;
}

export function limitCheck(text: string, max: number, source = "design_default"): LimitCheck {
  const length = [...text].length;
  return { length, max, over: length > max, source };
}

/** The hashtags as typed in a text field: split on spaces and commas, without the prefix. */
export function parseHashtags(input: string): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of input.split(/[\s,]+/)) {
    const tag = raw.replace(/^[#＃]+/, "").trim();
    if (tag && !seen.has(tag.toLowerCase())) {
      seen.add(tag.toLowerCase());
      out.push(tag);
    }
  }
  return out;
}

// ---------------------------------------------------------------------- exports
export interface ExportState {
  render?: { provenance_mode: string; is_proxy: boolean; status: string; preset_id: string } | null;
  versionState: string;
  presets: readonly string[];
  packaging?: { status: string; platform: string } | null;
  requireApprovedPackaging: boolean;
  checklist: readonly { key: string; label: string; required: boolean }[];
  checked: Record<string, boolean>;
  blockingClaims: number;
  pendingTranslations: readonly string[];
  chosenLanguages: readonly string[] | null;
}

/** Why the export button is disabled, mirroring the API's export rules (it still decides). */
export function exportBlockers(s: ExportState): string[] {
  const out: string[] = [];
  if (!s.render || s.render.is_proxy || s.render.status !== "ready") out.push("Choose a finished final render.");
  else if (s.render.provenance_mode !== "real")
    out.push("This render carries mock provenance (MOCK PROVENANCE — NOT FOR DISTRIBUTION) and cannot be exported.");
  else if (!s.presets.includes(s.render.preset_id))
    out.push(`The render preset is not one of this platform's presets.`);
  if (s.versionState !== "ready")
    out.push(`Only a ready version can be exported (this one is ${humanize(s.versionState)}).`);
  if (s.requireApprovedPackaging && s.packaging?.status !== "approved")
    out.push("Approve the packaging for this platform first.");
  for (const item of s.checklist) {
    if (item.required && !s.checked[item.key]) out.push(`Confirm: ${item.label}`);
  }
  if (s.blockingClaims > 0) out.push(`${s.blockingClaims} unsupported claim(s) block the export.`);
  for (const language of s.pendingTranslations) {
    if (s.chosenLanguages?.includes(language)) out.push(`The ${language} translation is not approved.`);
  }
  return out;
}

// ---------------------------------------------------------------------- spec templates
export const TEMPLATE_KINDS = ["video", "scene", "creator_style", "camera", "caption", "brand"] as const;

/** A template body as a list of "slot = value" lines for display. */
export function templateSlots(body: Record<string, unknown>, prefix = ""): { path: string; value: string }[] {
  const out: { path: string; value: string }[] = [];
  for (const [key, value] of Object.entries(body)) {
    const path = `${prefix}/${key}`;
    if (value && typeof value === "object" && !Array.isArray(value) && !("preset_id" in value)) {
      const nested = value as Record<string, unknown>;
      if (path === "/render/reframe") out.push({ path, value: JSON.stringify(nested) });
      else out.push(...templateSlots(nested, path));
    } else {
      out.push({
        path,
        value: Array.isArray(value)
          ? value.map((v) => (typeof v === "object" ? JSON.stringify(v) : String(v))).join(", ")
          : String(value),
      });
    }
  }
  return out;
}
