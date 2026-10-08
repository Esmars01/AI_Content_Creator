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

describe("operations console", () => {
  it("never shows a missing measurement as zero", async () => {
    const { NOT_REPORTED, metric, telemetryFacts } = await import("./gpu");
    expect(metric(undefined, " %")).toBe(NOT_REPORTED);
    expect(metric(0, " %", 0)).toBe("0 %"); // a measured zero is a zero
    const silent = telemetryFacts({ telemetry: {}, telemetry_at: null } as never);
    expect(silent).toEqual([{ label: "Telemetry", value: NOT_REPORTED }]);
    const partial = telemetryFacts({ telemetry: { gpu_util_pct: 87 }, telemetry_at: "2026-10-08T10:00:00Z" } as never);
    expect(partial.find((f) => f.label === "GPU")?.value).toBe("87 %");
    expect(partial.find((f) => f.label === "VRAM")?.value).toBe(NOT_REPORTED);
    expect(partial.find((f) => f.label === "Disk")?.value).toBe(NOT_REPORTED);
  });

  it("ages timestamps and the provider's view", async () => {
    const { ageText, providerText } = await import("./gpu");
    const now = Date.parse("2026-10-08T10:10:00Z");
    expect(ageText("2026-10-08T10:09:30Z", now)).toBe("30 s ago");
    expect(ageText(null, now)).toBe("never");
    expect(providerText({ provider_status: {}, provider_checked_at: null } as never)).toBe("not checked yet");
  });

  it("lists model states with failures first and download progress", async () => {
    const { modelStates } = await import("./gpu");
    const states = modelStates({
      model_states: {
        b: { state: "ready" },
        a: { state: "downloading", bytes_done: 5e9, bytes_total: 20e9, speed_mbps: 250, eta_s: 60 },
        c: { state: "failed", error: "checksum mismatch" },
      },
    } as never);
    expect(states.map((s) => s.key)).toEqual(["c", "a", "b"]);
    expect(states[1]?.detail).toBe("25 % of 20.0 GB · 250 MB/s · ETA 60 s");
  });

  it("offers loaded rows and free plugins, never a default", async () => {
    const { provisionChoices } = await import("./gpu");
    const choices = provisionChoices({
      rows: [
        { id: "r1", name: "Vast EU", kind: "vast", enabled: true, loaded: true, paid: true },
        { id: "r2", name: "Off", kind: "runpod_pod", enabled: false, loaded: false, paid: true },
      ],
      registered: [
        { key: "vast", name: "Vast", paid: true, mock: false },
        { key: "local", name: "Local", paid: false, mock: false },
        { key: "runpod_pod", name: "RunPod", paid: true, mock: false },
      ],
      skipped: {},
    } as never);
    expect(choices.map((c) => c.value)).toEqual(["row:r1", "key:local"]);
    expect(choices[0]?.paid).toBe(true);
    expect(provisionChoices(undefined)).toEqual([]);
  });
});
