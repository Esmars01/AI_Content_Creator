/** GPU fleet display logic (§31 GPU page, Phase 9). Pure functions: the page only renders them. */
import type { Schemas } from "./api";

export type Pool = Schemas["PoolOut"];
export type Worker = Schemas["WorkerOut"];
export type Spend = Record<string, number>;

export type PoolState = "disabled" | "static" | "scaling up" | "scaling down" | "steady" | "no provider";

/** What the fleet manager is doing with a pool right now, from its desired and current sizes. */
export function poolState(pool: Pool): PoolState {
  if (!pool.enabled) return "disabled";
  if (!pool.autoscale) return "static";
  if (pool.providers_available.length === 0) return "no provider";
  if (pool.desired > pool.current) return "scaling up";
  if (pool.desired < pool.current) return "scaling down";
  return "steady";
}

/** Today's spend against the daily budget: the projection decides the tone (holds start above 100 %). */
export function spendState(spend: Spend | null | undefined): {
  label: string;
  fraction: number;
  tone: "success" | "warning" | "danger" | "info";
} {
  if (!spend) return { label: "Spend unavailable (the scheduler did not answer)", fraction: 0, tone: "info" };
  const budget = spend.budget_daily_usd ?? 0;
  const spent = spend.spent_usd ?? 0;
  const projected = spend.projected_usd ?? spent;
  if (budget <= 0) return { label: `${spent.toFixed(2)} USD today (no daily budget)`, fraction: 0, tone: "info" };
  const fraction = projected / budget;
  const tone = fraction > 1 ? "danger" : fraction > 0.8 ? "warning" : "success";
  return {
    label: `${spent.toFixed(2)} USD spent, ${projected.toFixed(2)} USD projected of ${budget.toFixed(2)} USD today`,
    fraction: Math.min(fraction, 1),
    tone,
  };
}

const HOLD_LABELS: Record<string, string> = {
  budget_daily: "low priority, daily budget",
  budget_video: "video budget reached",
  budget_project: "project budget reached",
};

export function holdLabel(reason: string | null | undefined): string {
  if (!reason) return "";
  return HOLD_LABELS[reason] ?? reason;
}

/** Cold start (provision request → registration), honest about workers that never registered. */
export function coldStart(worker: Pick<Worker, "cold_start_s" | "provisioned_at" | "registered_at">): string {
  if (worker.cold_start_s != null) return `${Math.round(worker.cold_start_s)} s`;
  if (worker.provisioned_at && !worker.registered_at) return "not registered yet";
  return "—";
}

/** A worker the fleet manager provisioned (it may stop it); static workers are started elsewhere. */
export function isFleetWorker(worker: Pick<Worker, "pool_id">): boolean {
  return Boolean(worker.pool_id);
}

// ---------------------------------------------------------------------- operations console (cutover)
export type WorkerScope = "active" | "live" | "stopped" | "failed" | "terminated";
export const WORKER_SCOPES: { id: WorkerScope; label: string }[] = [
  { id: "active", label: "All but terminated" },
  { id: "live", label: "Live" },
  { id: "stopped", label: "Stopped" },
  { id: "failed", label: "Failed" },
  { id: "terminated", label: "Terminated" },
];

export const NOT_REPORTED = "not reported";

/** A measured value with its unit; a value the worker did not report is said so, never shown as 0. */
export function metric(value: unknown, unit: string, digits = 1): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return NOT_REPORTED;
  return `${value.toFixed(digits)}${unit}`;
}

type Telemetry = {
  gpu_util_pct?: number;
  vram_used_gb?: number;
  vram_total_gb?: number;
  gpu_temp_c?: number;
  gpu_power_w?: number;
  disk?: { free_gb?: number; total_gb?: number };
  cache?: { size_gb?: number; entries?: number; max_gb?: number };
};

/** The telemetry line of a worker: GPU, VRAM, temperature, disk and model cache, honest about gaps. */
export function telemetryFacts(worker: Pick<Worker, "telemetry" | "telemetry_at">): { label: string; value: string }[] {
  const t = (worker.telemetry ?? {}) as Telemetry;
  if (!worker.telemetry_at) return [{ label: "Telemetry", value: NOT_REPORTED }];
  const vram =
    typeof t.vram_used_gb === "number" && typeof t.vram_total_gb === "number"
      ? `${t.vram_used_gb.toFixed(1)} / ${t.vram_total_gb.toFixed(1)} GB`
      : NOT_REPORTED;
  const disk =
    typeof t.disk?.free_gb === "number" && typeof t.disk.total_gb === "number"
      ? `${t.disk.free_gb.toFixed(0)} GB free of ${t.disk.total_gb.toFixed(0)} GB`
      : NOT_REPORTED;
  const cache =
    typeof t.cache?.size_gb === "number"
      ? `${t.cache.size_gb.toFixed(1)} GB, ${t.cache.entries ?? 0} entries`
      : NOT_REPORTED;
  return [
    { label: "GPU", value: metric(t.gpu_util_pct, " %", 0) },
    { label: "VRAM", value: vram },
    { label: "Temp", value: metric(t.gpu_temp_c, " °C", 0) },
    { label: "Disk", value: disk },
    { label: "Model cache", value: cache },
  ];
}

/** Seconds since a timestamp, for "last heartbeat 12 s ago"; null when there is none. */
export function ageSeconds(at: string | null | undefined, now: number = Date.now()): number | null {
  if (!at) return null;
  const then = Date.parse(at);
  return Number.isNaN(then) ? null : Math.max(0, Math.round((now - then) / 1000));
}

export function ageText(at: string | null | undefined, now: number = Date.now()): string {
  const age = ageSeconds(at, now);
  if (age === null) return "never";
  if (age < 120) return `${age} s ago`;
  if (age < 7200) return `${Math.round(age / 60)} min ago`;
  return `${Math.round(age / 3600)} h ago`;
}

/** The provider's last view of a worker's instance. */
export function providerText(worker: Pick<Worker, "provider_status" | "provider_checked_at">): string {
  const status = worker.provider_status as { state?: string } | undefined;
  if (!status?.state || !worker.provider_checked_at) return "not checked yet";
  return `${status.state} (checked ${ageText(worker.provider_checked_at)})`;
}

export type ModelState = { key: string; state: string; detail: string };

const MODEL_STATE_ORDER = [
  "failed",
  "downloading",
  "verifying",
  "loading",
  "installed",
  "warm",
  "ready",
  "not_installed",
];

/** A worker's models and their preparation state, failures first. */
export function modelStates(worker: Pick<Worker, "model_states">): ModelState[] {
  const states = (worker.model_states ?? {}) as Record<string, Record<string, unknown>>;
  return Object.entries(states)
    .map(([key, s]) => {
      const done = typeof s.bytes_done === "number" ? s.bytes_done : null;
      const total = typeof s.bytes_total === "number" ? s.bytes_total : null;
      const parts: string[] = [];
      if (done !== null && total)
        parts.push(`${((100 * done) / total).toFixed(0)} % of ${(total / 1e9).toFixed(1)} GB`);
      if (typeof s.speed_mbps === "number") parts.push(`${s.speed_mbps.toFixed(0)} MB/s`);
      if (typeof s.eta_s === "number") parts.push(`ETA ${Math.round(s.eta_s)} s`);
      if (typeof s.error === "string") parts.push(s.error);
      return { key, state: String(s.state ?? "unknown"), detail: parts.join(" · ") };
    })
    .sort(
      (a, b) => MODEL_STATE_ORDER.indexOf(a.state) - MODEL_STATE_ORDER.indexOf(b.state) || a.key.localeCompare(b.key),
    );
}

/** The options of the provision form: configured, loaded providers and the free ones without a row. */
export function provisionChoices(
  providers: Schemas["ProvidersOut"] | undefined,
): { value: string; label: string; paid: boolean; providerId?: string }[] {
  if (!providers) return [];
  const rows = providers.rows
    .filter((r) => r.enabled && r.loaded !== false)
    .map((r) => ({ value: `row:${r.id}`, label: `${r.name} (${r.kind})`, paid: Boolean(r.paid), providerId: r.id }));
  const withRow = new Set(providers.rows.map((r) => r.kind));
  const free = providers.registered
    .filter((r) => !r.paid && !withRow.has(r.key))
    .map((r) => ({ value: `key:${r.key}`, label: `${r.name}${r.mock ? " (simulated)" : ""}`, paid: false }));
  return [...rows, ...free];
}

/** A node's GPU stage as one line: "Downloading the model — 25 % of 100.0 GB, ETA 30 s". */
export function stageText(gpu: Record<string, unknown> | null | undefined): string | null {
  if (!gpu || typeof gpu.stage !== "string") return null;
  const label = typeof gpu.label === "string" ? gpu.label : gpu.stage.replaceAll("_", " ");
  const parts: string[] = [];
  const done = typeof gpu.bytes_done === "number" ? gpu.bytes_done : null;
  const total = typeof gpu.bytes_total === "number" ? gpu.bytes_total : null;
  if (done !== null && total) parts.push(`${Math.round((100 * done) / total)} % of ${(total / 1e9).toFixed(1)} GB`);
  else if (typeof gpu.progress === "number" && gpu.progress > 0) parts.push(`${Math.round(gpu.progress * 100)} %`);
  if (typeof gpu.eta_s === "number") parts.push(`ETA ${Math.round(gpu.eta_s)} s`);
  if (typeof gpu.held_reason === "string") parts.push(holdLabel(gpu.held_reason));
  return parts.length ? `${label} — ${parts.join(", ")}` : label;
}
