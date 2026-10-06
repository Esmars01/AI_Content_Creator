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
