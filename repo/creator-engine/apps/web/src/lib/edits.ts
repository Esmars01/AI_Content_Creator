/**
 * Edit proposals, structured editors, locks and versions (§28, §31): display logic and the
 * operations the Advanced editors send. Pure functions; the components render them.
 */
import type { Schemas } from "./api";
import { humanize } from "./format";

type Json = Record<string, unknown>;

export type EditProposal = Schemas["EditOut"];
export type Operation = Json & { op: string; reason?: string };

// ------------------------------------------------------------------------------- proposals

export interface ImpactSummary {
  regenerate: string[];
  cascade: string[];
  keep: number;
  removed: string[];
  noVisibleEffect: { key: string; reason: string }[];
  generation: string[];
  modelNodes: string[];
  locksBlocking: { label: string; reason: string }[];
  estimateUsd: number | null;
  gpuSeconds: number | null;
  notes: string[];
  assumptions: string[];
  issues: { code: string; message: string; path?: string | null }[];
  planner: string | null;
}

const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const strings = (value: unknown): string[] => list(value).map(String);
const num = (value: unknown): number | null => (typeof value === "number" && Number.isFinite(value) ? value : null);

export function summarizeImpact(impact: Json | null | undefined): ImpactSummary {
  const i = impact ?? {};
  const estimate = (i.estimate ?? {}) as Json;
  return {
    regenerate: strings(i.regenerate),
    cascade: strings(i.cascade),
    keep: list(i.keep).length,
    removed: strings(i.removed),
    noVisibleEffect: list(i.no_visible_effect).map((e) => {
      const entry = (e ?? {}) as Json;
      return {
        key: String(entry.node_key ?? entry.item_ref ?? ""),
        reason: String(entry.reason ?? ""),
      };
    }),
    generation: strings(i.generation),
    modelNodes: strings(i.model_nodes),
    locksBlocking: list(i.locks_blocking).map((e) => {
      const entry = (e ?? {}) as Json;
      return { label: String(entry.group ?? entry.node_key ?? ""), reason: String(entry.reason ?? "") };
    }),
    estimateUsd: num(estimate.usd),
    gpuSeconds: num(estimate.gpu_s ?? estimate.gpu_seconds),
    notes: strings(i.notes),
    assumptions: strings(i.assumptions),
    issues: list(i.issues).map((e) => {
      const entry = (e ?? {}) as Json;
      return {
        code: String(entry.code ?? "issue"),
        message: String(entry.message ?? ""),
        path: entry.path ? String(entry.path) : null,
      };
    }),
    planner: i.planner ? String(i.planner) : null,
  };
}

export interface DiffEntry {
  path: string;
  kind: string;
  area: string;
  before: unknown;
  after: unknown;
}

/** Diff entries grouped by area (acting, world, camera, script, …), in first-seen order. */
export function groupDiff(entries: unknown[]): [string, DiffEntry[]][] {
  const groups = new Map<string, DiffEntry[]>();
  for (const raw of entries) {
    const e = (raw ?? {}) as Json;
    const entry: DiffEntry = {
      path: String(e.path ?? ""),
      kind: String(e.kind ?? "changed"),
      area: String(e.area ?? "other"),
      before: e.before,
      after: e.after,
    };
    groups.set(entry.area, [...(groups.get(entry.area) ?? []), entry]);
  }
  return [...groups.entries()];
}

/** A short rendering of a diff value: scalars as they are, objects summarized. */
export function short(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value))
    return value.length <= 4 && value.every((v) => typeof v !== "object") ? value.join(", ") : `${value.length} items`;
  const obj = value as Json;
  if (typeof obj.label === "string")
    return `${obj.label}${typeof obj.intensity === "number" ? ` (${obj.intensity})` : ""}`;
  if (typeof obj.type === "string") return String(obj.type);
  return `{${Object.keys(obj).slice(0, 3).join(", ")}${Object.keys(obj).length > 3 ? ", …" : ""}}`;
}

export interface CoverageChange {
  itemRef: string;
  dimension: string;
  before: string | null;
  after: string | null;
  change: string;
  blockedByLock: string | null;
  noVisibleEffect: boolean;
  reason: string | null;
}

export function coverageChanges(delta: Json | null | undefined): CoverageChange[] {
  return list((delta ?? {}).items).map((raw) => {
    const e = (raw ?? {}) as Json;
    const level = (side: unknown) => {
      const s = side as Json | null | undefined;
      return s ? `${String(s.level)}${s.method ? ` · ${humanize(String(s.method))}` : ""}` : null;
    };
    return {
      itemRef: String(e.item_ref ?? ""),
      dimension: String(e.dimension ?? ""),
      before: level(e.before),
      after: level(e.after),
      change: String(e.change ?? ""),
      blockedByLock: e.blocked_by_lock ? String(e.blocked_by_lock) : null,
      noVisibleEffect: Boolean(e.no_visible_effect),
      reason: e.reason ? String(e.reason) : null,
    };
  });
}

/**
 * "/scenes[scn_reveal]/acting/states[st_2]/strategies/gaze" → "scn_reveal · st_2 · strategies.gaze":
 * the keys, then the last two field names after the last key (enough to tell `displayed.label`
 * from `felt.label`).
 */
export function readablePath(path: string): string {
  const keys = [...path.matchAll(/\[([^\]]+)\]/g)].map((m) => m[1] ?? "");
  const lastKey = path.lastIndexOf("]");
  const tail = (lastKey >= 0 ? path.slice(lastKey + 1) : path).split("/").filter(Boolean);
  const field = tail.slice(-2).join(".");
  return [...keys, field || null].filter(Boolean).join(" · ") || path;
}

/** One plain-language line per operation. */
export function describeOp(op: Operation): string {
  const scope = op.scope as Json | undefined;
  const where = scope?.scene_keys ? ` in ${strings(scope.scene_keys).join(", ")}` : "";
  switch (op.op) {
    case "set_acting": {
      const changes = (op.changes ?? {}) as Json;
      const emotion = ((changes.emotion ?? {}) as Json).displayed as Json | undefined;
      const parts = [
        emotion?.label ? `displayed ${String(emotion.label)}` : null,
        typeof emotion?.intensity_delta === "number"
          ? `intensity ${emotion.intensity_delta > 0 ? "+" : ""}${emotion.intensity_delta}`
          : null,
        ...Object.entries((changes.strategies ?? {}) as Json).map(
          ([k, v]) => `${humanize(k)} → ${humanize(String(v))}`,
        ),
      ].filter(Boolean);
      return `Acting${where}: ${parts.join(", ") || "changed"}`;
    }
    case "add_behavior_event":
      return `Add ${humanize(String(((op.event ?? {}) as Json).type ?? "event"))}${where}`;
    case "remove_behavior_event":
      return `Remove event ${String(op.event_key ?? "")}`;
    case "set_camera": {
      const added = list(op.add_moves).map((m) => humanize(String((m as Json).type)));
      return `Camera${where}: ${added.length ? `add ${added.join(", ")}` : "change"}`;
    }
    case "set_world_binding":
      return `Move${where || " every scene"} to ${String(op.world_query ?? op.world_version_id ?? "another world")}`;
    case "set_world_override":
      return `World overrides${where}`;
    case "set_wardrobe":
      return `Outfit${where}: ${String(op.wardrobe_query ?? op.wardrobe_version_id ?? "another outfit")}`;
    case "set_cast":
      return `Voice or appearance: ${String(op.voice_query ?? op.voice_version_id ?? op.appearance_version_id ?? "change")}`;
    case "set_pacing":
      return `Pacing${where}: ${[
        typeof op.target_wpm_delta === "number" ? `${op.target_wpm_delta > 0 ? "faster" : "slower"} speech` : null,
        op.cut_cadence ? `${String(op.cut_cadence)} cuts` : null,
      ]
        .filter(Boolean)
        .join(", ")}`;
    case "set_lock":
      return `Locks: ${[...list(op.add).map((l) => `+${String((l as Json).group)}`), ...list(op.remove).map((l) => `−${String((l as Json).group)}`)].join(" ")}`;
    case "regenerate":
      return `Regenerate ${strings(op.components).map(humanize).join(", ")}${where} (${String(op.seed_policy ?? "new")} seed)`;
    case "select_take":
      return `Select ${String(op.take_key ?? "the ranked take")} for ${String(op.shot_key)}`;
    case "reroute":
      return `Re-route ${strings(op.node_keys).join(", ")}`;
    default:
      return humanize(op.op);
  }
}

// ------------------------------------------------------------------------------- structured editors

export interface StateDraft {
  key: string;
  sceneKey: string;
  displayed: string;
  intensity: number;
  felt: string;
  feltIntensity: number;
  masking: boolean;
  strategies: Record<string, string>;
  attentionTarget: string;
}

export const STRATEGY_FIELDS = ["prosody", "gaze", "gesture", "posture", "reaction", "camera_awareness"] as const;

/** Drafts of every acting state of the spec (the Advanced performance editor's initial values). */
export function stateDrafts(spec: Json): StateDraft[] {
  const out: StateDraft[] = [];
  for (const scene of list(spec.scenes) as Json[]) {
    const acting = scene.acting as Json | null;
    for (const state of list(acting?.states) as Json[]) {
      const emotion = (state.emotion ?? {}) as Json;
      const displayed = (emotion.displayed ?? {}) as Json;
      const felt = (emotion.felt ?? {}) as Json;
      const strategies = (state.strategies ?? {}) as Json;
      out.push({
        key: String(state.key),
        sceneKey: String(scene.key),
        displayed: String(displayed.label ?? ""),
        intensity: Number(displayed.intensity ?? 0.5),
        felt: String(felt.label ?? ""),
        feltIntensity: Number(felt.intensity ?? 0.5),
        masking: Boolean(emotion.masking),
        strategies: Object.fromEntries(STRATEGY_FIELDS.map((f) => [f, String(strategies[f] ?? "")])),
        attentionTarget: typeof state.attention_target === "string" ? state.attention_target : "",
      });
    }
  }
  return out;
}

const round = (n: number) => Math.round(n * 100) / 100;

/** `set_acting` operations for the fields a draft changed (one per state; nothing for unchanged ones). */
export function actingOps(original: StateDraft[], drafts: StateDraft[]): Operation[] {
  const before = new Map(original.map((d) => [d.key, d]));
  const ops: Operation[] = [];
  for (const draft of drafts) {
    const was = before.get(draft.key);
    if (!was) continue;
    const changes: Json = {};
    const displayed: Json = {};
    if (draft.displayed !== was.displayed) displayed.label = draft.displayed;
    if (round(draft.intensity) !== round(was.intensity)) displayed.intensity = round(draft.intensity);
    const felt: Json = {};
    if (draft.felt !== was.felt) felt.label = draft.felt;
    if (round(draft.feltIntensity) !== round(was.feltIntensity)) felt.intensity = round(draft.feltIntensity);
    const emotion: Json = {};
    if (Object.keys(displayed).length) emotion.displayed = displayed;
    if (Object.keys(felt).length) emotion.felt = felt;
    if (draft.masking !== was.masking) emotion.masking = draft.masking;
    if (Object.keys(emotion).length) changes.emotion = emotion;
    const strategies = Object.fromEntries(
      STRATEGY_FIELDS.filter((f) => draft.strategies[f] && draft.strategies[f] !== was.strategies[f]).map((f) => [
        f,
        draft.strategies[f],
      ]),
    );
    if (Object.keys(strategies).length) changes.strategies = strategies;
    if (draft.attentionTarget && draft.attentionTarget !== was.attentionTarget)
      changes.attention_target = draft.attentionTarget;
    if (Object.keys(changes).length) {
      ops.push({
        op: "set_acting",
        scope: { scene_keys: [draft.sceneKey], state_keys: [draft.key] },
        changes,
        reason: "edited in the performance editor",
      });
    }
  }
  return ops;
}

export interface EventDraft {
  key: string;
  sceneKey: string;
  type: string;
  intensity: number;
  remove: boolean;
}

export function eventDrafts(spec: Json): EventDraft[] {
  return (list(spec.scenes) as Json[]).flatMap((scene) =>
    (list((scene.acting as Json | null)?.events) as Json[]).map((e) => ({
      key: String(e.key),
      sceneKey: String(scene.key),
      type: String(e.type),
      intensity: Number(e.intensity ?? 0.5),
      remove: false,
    })),
  );
}

export function eventOps(original: EventDraft[], drafts: EventDraft[]): Operation[] {
  const before = new Map(original.map((d) => [d.key, d]));
  const ops: Operation[] = [];
  for (const draft of drafts) {
    const was = before.get(draft.key);
    if (!was) continue;
    if (draft.remove) {
      ops.push({
        op: "remove_behavior_event",
        scene_key: draft.sceneKey,
        event_key: draft.key,
        reason: "removed in the events editor",
      });
    } else if (round(draft.intensity) !== round(was.intensity)) {
      ops.push({
        op: "set_behavior_event",
        scene_key: draft.sceneKey,
        event_key: draft.key,
        changes: { intensity: round(draft.intensity) },
        reason: "edited in the events editor",
      });
    }
  }
  return ops;
}

export const SCENE_INTENT_FIELDS = [
  "narrative_goal",
  "emotional_goal",
  "audience_effect",
  "persuasion_goal",
  "information_goal",
  "attention_goal",
  "reveal_strategy",
  "performance_strategy",
] as const;

export type IntentDraft = Record<string, Record<string, string>>; // scene key → field → token

export function intentDrafts(spec: Json): IntentDraft {
  return Object.fromEntries(
    (list(spec.scenes) as Json[]).map((scene) => {
      const intent = (scene.intent ?? {}) as Json;
      return [String(scene.key), Object.fromEntries(SCENE_INTENT_FIELDS.map((f) => [f, String(intent[f] ?? "")]))];
    }),
  );
}

export function intentOps(original: IntentDraft, draft: IntentDraft): Operation[] {
  const ops: Operation[] = [];
  for (const [scene, fields] of Object.entries(draft)) {
    const changed = Object.fromEntries(
      Object.entries(fields)
        .filter(([f, v]) => v !== (original[scene]?.[f] ?? ""))
        .map(([f, v]) => [f, v || null]),
    );
    if (Object.keys(changed).length) {
      ops.push({
        op: "set_intent",
        scope: { scene_keys: [scene] },
        fields: changed,
        reason: "edited in the intent editor",
      });
    }
  }
  return ops;
}

// ------------------------------------------------------------------------------- locks

export interface LockRow {
  group: string;
  scope: { scene_keys: string[] | null; character_keys: string[] | null; shot_keys: string[] | null };
  setBy: string;
}

export function locksOf(spec: Json): LockRow[] {
  return (list(spec.locks) as Json[]).map((lock) => {
    const scope = (lock.scope ?? {}) as Json;
    const keys = (name: string) => (Array.isArray(scope[name]) ? strings(scope[name]) : null);
    return {
      group: String(lock.group),
      scope: { scene_keys: keys("scene_keys"), character_keys: keys("character_keys"), shot_keys: keys("shot_keys") },
      setBy: String(lock.set_by ?? "user"),
    };
  });
}

export function lockLabel(lock: LockRow): string {
  const parts = [
    lock.scope.scene_keys ? lock.scope.scene_keys.join(", ") : null,
    lock.scope.character_keys ? lock.scope.character_keys.join(", ") : null,
    lock.scope.shot_keys ? lock.scope.shot_keys.join(", ") : null,
  ].filter(Boolean);
  return `${humanize(lock.group)}${parts.length ? ` (${parts.join("; ")})` : " (whole video)"}`;
}

export const sameLock = (a: LockRow, b: LockRow) =>
  a.group === b.group && JSON.stringify(a.scope) === JSON.stringify(b.scope);

/** The PUT body: the complete set of locks. */
export function locksBody(rows: LockRow[]): { locks: { group: string; scope: LockRow["scope"] }[] } {
  return { locks: rows.map((r) => ({ group: r.group, scope: r.scope })) };
}

// ------------------------------------------------------------------------------- versions

export type VersionSummary = Schemas["VersionSummary"];

export interface VersionNode {
  version: VersionSummary;
  children: VersionNode[];
  depth: number;
}

/** Versions as a tree by parent (a parent from another video — a duplicate — is a root); roots and
 * children in version-number order. */
export function versionTree(versions: VersionSummary[]): VersionNode[] {
  const byId = new Map(versions.map((v) => [v.id, v]));
  const children = new Map<string, VersionSummary[]>();
  const roots: VersionSummary[] = [];
  for (const v of [...versions].sort((a, b) => a.number - b.number)) {
    if (v.parent_version_id && byId.has(v.parent_version_id)) {
      children.set(v.parent_version_id, [...(children.get(v.parent_version_id) ?? []), v]);
    } else {
      roots.push(v);
    }
  }
  const build = (v: VersionSummary, depth: number): VersionNode => ({
    version: v,
    depth,
    children: (children.get(v.id) ?? []).map((c) => build(c, depth + 1)),
  });
  return roots.map((r) => build(r, 0));
}

export function flatten(nodes: VersionNode[]): VersionNode[] {
  return nodes.flatMap((n) => [n, ...flatten(n.children)]);
}

export const RESUMABLE = new Set(["partial", "failed", "cancelled"]);

/**
 * The edit panel's time range: both empty (the whole video), or a complete range with the end after
 * the start. A half-filled or inverted range is an error to show, never silently dropped (which
 * would propose the edit for the whole video).
 */
export function timeRange(range: { start: string; end: string }): {
  value: [number, number] | null;
  error: string | null;
} {
  const start = range.start.trim();
  const end = range.end.trim();
  if (!start && !end) return { value: null, error: null };
  if (!start || !end) return { value: null, error: "Enter both a start and an end, or leave both empty." };
  const a = Number(start);
  const b = Number(end);
  if (!Number.isFinite(a) || !Number.isFinite(b)) return { value: null, error: "Enter the times in seconds." };
  if (a < 0) return { value: null, error: "The start cannot be negative." };
  if (b <= a) return { value: null, error: "The end must be after the start." };
  return { value: [a, b], error: null };
}
