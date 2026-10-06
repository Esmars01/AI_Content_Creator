import { describe, expect, it } from "vitest";

import { coldStart, holdLabel, isFleetWorker, type Pool, poolState, spendState } from "./gpu";

const pool = (over: Partial<Pool>): Pool =>
  ({
    id: "p",
    enabled: true,
    autoscale: true,
    providers_available: ["mock"],
    desired: 1,
    current: 1,
    ...over,
  }) as Pool;

describe("pool state", () => {
  it("follows desired against current", () => {
    expect(poolState(pool({ desired: 2, current: 1 }))).toBe("scaling up");
    expect(poolState(pool({ desired: 0, current: 1 }))).toBe("scaling down");
    expect(poolState(pool({}))).toBe("steady");
  });
  it("names pools the fleet does not scale", () => {
    expect(poolState(pool({ enabled: false }))).toBe("disabled");
    expect(poolState(pool({ autoscale: false }))).toBe("static");
    expect(poolState(pool({ providers_available: [] }))).toBe("no provider");
  });
});

describe("spend", () => {
  it("turns red when the projection exceeds the budget", () => {
    const s = spendState({ spent_usd: 15, projected_usd: 22, budget_daily_usd: 20 });
    expect(s.tone).toBe("danger");
    expect(s.fraction).toBe(1);
    expect(s.label).toContain("22.00 USD projected of 20.00 USD");
  });
  it("warns above 80 % and is honest when unknown", () => {
    expect(spendState({ spent_usd: 10, projected_usd: 17, budget_daily_usd: 20 }).tone).toBe("warning");
    expect(spendState({ spent_usd: 1, projected_usd: 2, budget_daily_usd: 20 }).tone).toBe("success");
    expect(spendState(null).label).toMatch(/unavailable/);
    expect(spendState({ spent_usd: 3, budget_daily_usd: 0 }).label).toMatch(/no daily budget/);
  });
});

describe("workers", () => {
  it("reports cold starts and unregistered hosts", () => {
    expect(coldStart({ cold_start_s: 61.4, provisioned_at: "x", registered_at: "y" })).toBe("61 s");
    expect(coldStart({ cold_start_s: null, provisioned_at: "x", registered_at: null })).toBe("not registered yet");
    expect(coldStart({ cold_start_s: null, provisioned_at: null, registered_at: null })).toBe("—");
  });
  it("tells fleet workers from static ones", () => {
    expect(isFleetWorker({ pool_id: "consumer" })).toBe(true);
    expect(isFleetWorker({ pool_id: null })).toBe(false);
  });
  it("labels holds", () => {
    expect(holdLabel("budget_daily")).toBe("low priority, daily budget");
    expect(holdLabel(null)).toBe("");
  });
});
