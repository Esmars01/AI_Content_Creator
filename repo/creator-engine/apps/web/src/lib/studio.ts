/** Creator Studio and World Studio display logic (§31, Phase 10). Pure functions; the pages render them. */

export type Json = Record<string, unknown>;

/** Creator DNA editor tabs (§31) → the DNA sections they edit. */
export const CREATOR_DNA_TABS = [
  { id: "identity", label: "Identity & Canon", keys: ["identity"] },
  { id: "personality", label: "Personality", keys: ["personality"] },
  { id: "speech", label: "Speech", keys: ["speech"] },
  { id: "behavior", label: "Behavior", keys: ["behavior"] },
  { id: "gesture", label: "Gesture & Posture", keys: ["gesture_posture", "gesture"] },
  { id: "gaze", label: "Gaze", keys: ["gaze"] },
  { id: "camera", label: "Camera", keys: ["camera"] },
  { id: "fashion", label: "Fashion", keys: ["fashion"] },
  { id: "worlds", label: "Worlds", keys: ["world", "worlds"] },
  { id: "editing", label: "Editing", keys: ["editing"] },
  { id: "avoidances", label: "Avoidances", keys: ["avoidances"] },
] as const;

/** World DNA sections in the World Studio editor (the floor plan is drawn from elements, zones, positions). */
export const WORLD_DNA_SECTIONS = [
  "name",
  "kind",
  "style_tags",
  "palette",
  "geometry",
  "zones",
  "elements",
  "background_layouts",
  "lighting",
  "time_and_weather",
  "acoustics",
  "camera_positions",
  "continuity",
  "references",
] as const;

/** The DNA key a tab edits (the first one present in this DNA, else the first listed). */
export function sectionKey(dna: Json, keys: readonly string[]): string {
  return keys.find((k) => k in dna) ?? keys[0] ?? "";
}

export type FieldKind = "number" | "string" | "boolean" | "json";

/** How the Simple view edits a value: scalars inline, structures as JSON. */
export function fieldKind(value: unknown): FieldKind {
  if (typeof value === "number") return "number";
  if (typeof value === "string") return "string";
  if (typeof value === "boolean") return "boolean";
  return "json";
}

/** Parses a JSON field; returns an error message instead of throwing. */
export function parseJson(text: string): { value?: unknown; error?: string } {
  try {
    return { value: JSON.parse(text) };
  } catch (error) {
    return { error: error instanceof Error ? error.message : "invalid JSON" };
  }
}

export type VersionRef = { id: string; number: number; status: string };

/** The version the editor works on: the newest draft, if any (edits always happen on a draft). */
export function editableDraft<T extends VersionRef>(versions: readonly T[]): T | null {
  const drafts = versions.filter((v) => v.status === "draft");
  return drafts.length ? drafts.reduce((a, b) => (b.number > a.number ? b : a)) : null;
}

export type ScorecardRow = {
  key: string;
  label: string;
  value: string;
  note: string;
  mock: boolean;
  measured: boolean;
};

const SCORE_LABELS: Record<string, string> = {
  identity_similarity: "Identity similarity",
  voice_similarity: "Voice similarity",
  lipsync: "Lip sync (advisory)",
  wer: "WER",
  speech_quality: "Speech quality",
  wpm: "Measured WPM",
  accent: "Accent",
  world_fidelity: "World fidelity",
  human_rating: "Human rating",
  same_person_rating: "Same person?",
};

function fmt(value: unknown): string {
  return typeof value === "number" ? (Number.isInteger(value) ? String(value) : value.toFixed(3)) : String(value);
}

/** The scorecard as rows: measured values, rated values, or the reason it was not measured. */
export function scorecardRows(card: Json | null | undefined): ScorecardRow[] {
  if (!card) return [];
  return Object.entries(SCORE_LABELS)
    .filter(([key]) => key in card)
    .map(([key, label]) => {
      const metric = (card[key] ?? {}) as Json;
      const status = String(metric.status ?? "");
      const mock = Boolean(metric.mock);
      if (status === "measured") {
        const value =
          "value" in metric ? fmt(metric.value) : `p50 ${fmt(metric.p50)} (min ${fmt(metric.min)}, n=${fmt(metric.n)})`;
        return { key, label, value, note: String(metric.method ?? metric.metric ?? ""), mock, measured: true };
      }
      if (status === "rated")
        return { key, label, value: `${metric.value} / 5`, note: "human rating", mock, measured: true };
      return { key, label, value: "not measured", note: String(metric.reason ?? ""), mock, measured: false };
    });
}

/** I9 at the viewer level: only confirmed outcomes count as delivered. */
export function coverageSummary(card: Json | null | undefined): string {
  const rvo = (card?.requested_vs_observed ?? {}) as { items?: number; confirmed?: number; status?: string };
  if (!rvo.items) return "no coverage report";
  return `${rvo.confirmed ?? 0} of ${rvo.items} requested items observed (confirmed)`;
}

export type PackImage = {
  asset_id: string;
  kind: string;
  label: string;
  similarity: number | null;
  decision: string;
  mock?: boolean;
};

/** Identity-pack images grouped for the gallery: angles, then expressions. */
export function groupPack(images: readonly PackImage[]): { angles: PackImage[]; expressions: PackImage[] } {
  return {
    angles: images.filter((i) => i.kind === "angle"),
    expressions: images.filter((i) => i.kind === "expression"),
  };
}

export type PlateCell = {
  position: string;
  time: string;
  weather: string;
  candidates: string[];
  chosen: string | null;
};

/** The plates gallery: one cell per position × time × weather that has candidates or a choice. */
export function plateCells(candidates: Json, plates: Json): PlateCell[] {
  const cells = new Map<string, PlateCell>();
  const add = (position: string, time: string, weather: string) => {
    const key = `${position}|${time}|${weather}`;
    if (!cells.has(key)) cells.set(key, { position, time, weather, candidates: [], chosen: null });
    return cells.get(key) as PlateCell;
  };
  for (const [position, byTime] of Object.entries(candidates ?? {}))
    for (const [time, byWeather] of Object.entries((byTime ?? {}) as Json))
      for (const [weather, ids] of Object.entries((byWeather ?? {}) as Json))
        add(position, time, weather).candidates.push(...((ids as string[]) ?? []));
  for (const [position, byTime] of Object.entries(plates ?? {}))
    for (const [time, byWeather] of Object.entries((byTime ?? {}) as Json))
      for (const [weather, id] of Object.entries((byWeather ?? {}) as Json))
        add(position, time, weather).chosen = String(id);
  return [...cells.values()].sort((a, b) =>
    `${a.position}${a.time}${a.weather}`.localeCompare(`${b.position}${b.time}${b.weather}`),
  );
}

export type FloorItem = {
  key: string;
  label: string;
  kind: "element" | "zone" | "camera";
  x: number;
  y: number;
  status?: string;
};

/** Floor-plan items (normalized x, y in 0..1 → SVG units of `size`). Items without a position are skipped. */
export function floorPlan(dna: Json, size = 300): FloorItem[] {
  const items: FloorItem[] = [];
  const place = (list: unknown, kind: FloorItem["kind"]) => {
    for (const raw of (list as Json[] | undefined) ?? []) {
      const position = raw.position as number[] | null | undefined;
      const [x, y] = position ?? [];
      if (x === undefined || y === undefined) continue;
      items.push({
        key: String(raw.key),
        label: String(raw.label || raw.key),
        kind,
        x: Math.round(x * size),
        y: Math.round(y * size),
        ...(raw.status ? { status: String(raw.status) } : {}),
      });
    }
  };
  place(dna.zones, "zone");
  place(dna.elements, "element");
  place(dna.camera_positions, "camera");
  return items;
}

export type MemoryLike = { status: string; pinned: boolean; conflict_state: string; category: string };

/** Memory items grouped by category; proposed and conflicting items first (they need the user). */
export function memoryGroups<T extends MemoryLike>(items: readonly T[]): [string, T[]][] {
  const rank = (i: T) => (i.conflict_state === "unresolved" ? 0 : i.status === "proposed" ? 1 : i.pinned ? 2 : 3);
  const groups = new Map<string, T[]>();
  for (const item of [...items].sort((a, b) => rank(a) - rank(b))) {
    groups.set(item.category, [...(groups.get(item.category) ?? []), item]);
  }
  return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
}

/** Which actions the Memory UI offers for an item (the API decides; this only hides the impossible). */
export function memoryActions(item: MemoryLike): string[] {
  if (item.status === "proposed") return ["activate", "dismiss"];
  if (item.status === "forgotten" || item.status === "dismissed" || item.status === "superseded") return [];
  const actions = [item.pinned ? "unpin" : "pin", "forget"];
  if (item.conflict_state === "unresolved") actions.push("resolve_conflict");
  return actions;
}
