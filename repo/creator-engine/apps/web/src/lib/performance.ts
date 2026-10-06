/**
 * The read-only Performance lane (§31): acting states as colored bands with emotion labels and
 * transition markers, plus event markers, placed by the plan report's state and event timings
 * (estimated at planning, measured after previz). Display logic only.
 */
import type { Schemas } from "./api";
import { humanize } from "./format";

type Json = Record<string, unknown>;

export interface StateTiming {
  state_key: string;
  start_s: number;
  end_s: number;
  requested_start_s?: number | null;
  drift_s?: number | null;
}

export interface EventTiming {
  scene_key: string;
  event_key: string;
  type: string;
  at_s: number;
  end_s?: number | null;
}

export interface Band {
  key: string;
  sceneKey: string;
  label: string;
  felt: string | null;
  masking: boolean;
  startS: number;
  endS: number;
  left: number;
  width: number;
  color: string;
  driftS: number | null;
  trigger: string | null;
  transition: string | null;
}

export interface Marker {
  key: string;
  label: string;
  atS: number;
  left: number;
  width: number;
}

export interface Lane {
  durationS: number;
  bands: Band[];
  transitions: Marker[];
  events: Marker[];
}

const EMOTION_COLORS: Record<string, string> = {
  confident: "#2563eb",
  excited: "#ea580c",
  curious: "#0891b2",
  surprised: "#c026d3",
  skeptical: "#7c3aed",
  uncertain: "#64748b",
  serious: "#334155",
  calm: "#0d9488",
  amused: "#ca8a04",
  embarrassed: "#db2777",
  determined: "#1d4ed8",
  warm: "#d97706",
  neutral: "#94a3b8",
};

export function emotionColor(label: string): string {
  const known = EMOTION_COLORS[label];
  if (known) return known;
  let hash = 0;
  for (const ch of label) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${hash % 360} 55% 42%)`;
}

const pct = (value: number, total: number) => (total > 0 ? Math.min(100, Math.max(0, (value / total) * 100)) : 0);

function get(obj: unknown, ...path: string[]): unknown {
  let cur: unknown = obj;
  for (const key of path) {
    if (!cur || typeof cur !== "object") return undefined;
    cur = (cur as Json)[key];
  }
  return cur;
}

/** Acting states of a spec, in scene order, with their scene key. */
export function actingStates(spec: Schemas["VersionOut"]["spec"]): { sceneKey: string; state: Json }[] {
  const scenes = (get(spec, "scenes") as Json[] | undefined) ?? [];
  return [...scenes]
    .sort((a, b) => Number(a.order ?? 0) - Number(b.order ?? 0))
    .flatMap((scene) =>
      ((get(scene, "acting", "states") as Json[] | undefined) ?? []).map((state) => ({
        sceneKey: String(scene.key),
        state,
      })),
    );
}

export function buildLane(
  spec: Schemas["VersionOut"]["spec"],
  stateTimings: readonly StateTiming[],
  eventTimings: readonly EventTiming[],
  durationS: number,
): Lane {
  const timing = new Map(stateTimings.map((t) => [t.state_key, t]));
  const total = Math.max(durationS, ...stateTimings.map((t) => t.end_s), 0.001);
  const bands: Band[] = [];
  const transitions: Marker[] = [];
  for (const { sceneKey, state } of actingStates(spec)) {
    const key = String(state.key);
    const t = timing.get(key);
    if (!t) continue;
    const label = String(get(state, "emotion", "displayed", "label") ?? "neutral");
    const felt = get(state, "emotion", "felt", "label");
    const trigger = get(state, "transition_in", "trigger", "kind");
    const style = get(state, "transition_in", "style");
    bands.push({
      key,
      sceneKey,
      label,
      felt: typeof felt === "string" && felt !== label ? felt : null,
      masking: Boolean(get(state, "emotion", "masking")),
      startS: t.start_s,
      endS: t.end_s,
      left: pct(t.start_s, total),
      width: Math.max(0.5, pct(t.end_s - t.start_s, total)),
      color: emotionColor(label),
      driftS: t.drift_s ?? null,
      trigger: typeof trigger === "string" ? trigger : null,
      transition: typeof style === "string" ? style : null,
    });
    if (typeof trigger === "string" || typeof style === "string") {
      transitions.push({
        key: `${key}:in`,
        label: [style, trigger]
          .filter((v) => typeof v === "string")
          .map((v) => humanize(v as string))
          .join(" · "),
        atS: t.start_s,
        left: pct(t.start_s, total),
        width: 0,
      });
    }
  }
  bands.sort((a, b) => a.startS - b.startS);
  const events: Marker[] = eventTimings.map((e) => ({
    key: e.event_key,
    label: humanize(e.type),
    atS: e.at_s,
    left: pct(e.at_s, total),
    width: e.end_s ? pct(e.end_s - e.at_s, total) : 0,
  }));
  return { durationS: total, bands, transitions, events };
}

/** The Simple view's trajectory in plain words: "confident → (realization) surprised → serious". */
export function trajectorySentence(lane: Pick<Lane, "bands">): string {
  return lane.bands
    .map((band) => {
      const shown = band.masking && band.felt ? `${band.label} (hiding ${band.felt})` : band.label;
      return band.trigger ? `(${humanize(band.trigger).toLowerCase()}) ${shown}` : shown;
    })
    .join(" → ");
}
