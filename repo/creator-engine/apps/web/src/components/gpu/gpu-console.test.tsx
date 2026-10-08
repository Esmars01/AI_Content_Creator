import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { Workers } = await import("./workers");
const { Provision } = await import("./provision");
const { Profiles } = await import("./profiles");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );

const stopped = {
  id: "11111111-1111-4111-8111-111111111111",
  external_id: "7000001",
  provider_kind: "vast",
  runtime_family: "wan",
  variant: "infinitetalk",
  pool_id: "admin:vast",
  gpu_type: "a100_80gb",
  region: "eu",
  price_per_hour_usd: "1.20",
  state: "stopped",
  telemetry: {},
  telemetry_at: null,
  provider_status: { state: "stopped" },
  provider_checked_at: "2026-10-08T10:00:00Z",
  model_states: { "infinitetalk-single": { state: "installed" } },
  actions: ["refresh", "start", "terminate"],
  last_heartbeat_at: null,
};

describe("GPU console", () => {
  afterEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("keeps stopped workers startable and never shows missing telemetry as zero", async () => {
    get.mockImplementation(() => ok([stopped]));
    post.mockImplementation(() => ok({ worker_id: stopped.id, state: "provisioning" }));
    wrap(<Workers admin />);
    await screen.findByText("7000001");
    expect(screen.getByText("Telemetry: not reported")).toBeTruthy();
    expect(screen.getByText("infinitetalk-single: Installed")).toBeTruthy();
    expect(screen.queryByText(/GPU: 0/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/admin/gpu/workers/{worker_id}:start", {
        params: { path: { worker_id: stopped.id } },
      }),
    );
  });

  it("terminates only after the typed confirmation", async () => {
    get.mockImplementation(() => ok([stopped]));
    post.mockImplementation(() => ok({ worker_id: stopped.id, state: "terminated" }));
    wrap(<Workers admin />);
    fireEvent.click(await screen.findByRole("button", { name: "Terminate" }));
    const panel = screen.getByRole("group", { name: "Terminate" });
    const confirm = within(panel).getByRole("button", { name: "Terminate" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.change(within(panel).getByLabelText(/to confirm/), { target: { value: "terminate" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/admin/gpu/workers/{worker_id}:stop", {
        params: { path: { worker_id: stopped.id } },
        body: { action: "terminate", confirm: stopped.id },
      }),
    );
  });

  it("members see no actions", async () => {
    get.mockImplementation(() => ok([stopped]));
    wrap(<Workers admin={false} />);
    await screen.findByText("7000001");
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
  });

  it("provisioning has no default provider and asks before renting a paid GPU", async () => {
    get.mockImplementation((path: string) =>
      path === "/v1/admin/gpu/providers"
        ? ok({
            rows: [
              {
                id: "p1",
                name: "Vast EU",
                kind: "vast",
                enabled: true,
                loaded: true,
                paid: true,
                regions: [],
                config: {},
              },
            ],
            registered: [{ key: "vast", name: "Vast", paid: true, mock: false }],
            skipped: {},
          })
        : ok([
            {
              provider: "vast",
              gpu_class: "a100_80gb",
              region: "eu",
              vram_gb: 80,
              price_per_hour_usd: 1.2,
              available: 1,
            },
          ]),
    );
    wrap(<Provision />);
    const provider = (await screen.findByLabelText("Provider")) as HTMLSelectElement;
    expect(provider.value).toBe("");
    const submit = screen.getByRole("button", { name: "Provision" }) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
    await screen.findByRole("option", { name: "Vast EU (vast) — paid" });
    fireEvent.change(provider, { target: { value: "row:p1" } });
    fireEvent.change(await screen.findByRole("combobox", { name: "GPU class" }), { target: { value: "a100_80gb" } });
    fireEvent.change(screen.getByLabelText("Runtime family"), { target: { value: "wan" } });
    expect(submit.disabled).toBe(true); // the paid acknowledgement is missing
    fireEvent.click(screen.getByLabelText(/billing starts now/));
    expect(submit.disabled).toBe(false);
  });

  it("shows a profile's computed disk and declared model sizes, and prepares workers", async () => {
    get.mockImplementation((path: string) =>
      path === "/v1/admin/gpu/profiles"
        ? ok([
            {
              id: "talking_head_a100_80gb",
              label: "Talking head",
              gpu_class: "a100_80gb",
              vram_gb: 80,
              colocate: true,
              image: "talking_head",
              components: [],
              enabled: true,
              sizing: {
                models: [
                  {
                    key: "infinitetalk-single",
                    adapter: "infinitetalk",
                    declared_gb: 86.92,
                    repo: "MeiGen-AI/InfiniteTalk",
                    revision: "d59847ebdacf19245bfca3fb",
                    license: "Apache-2.0",
                  },
                ],
                models_gb: 93.68,
                staging_gb: 14.05,
                scratch_gb: 40,
                image_gb: 35,
                disk_gb: 190,
                vram_gb: 80,
                vram_min_gb: 40,
                vram_recommended_gb: 80,
                fits: true,
                notes: ["sizes are declared"],
                missing: [],
              },
            },
          ])
        : ok({ rows: [], registered: [], skipped: {} }),
    );
    wrap(<Profiles />);
    expect(await screen.findByText("190 GB")).toBeTruthy();
    expect(screen.getByText("86.92 GB (declared)")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Provision this profile" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("asks an idle worker to prepare its models", async () => {
    const idle = { ...stopped, state: "idle", actions: ["refresh", "restart", "stop", "terminate"] };
    get.mockImplementation(() => ok([idle]));
    post.mockImplementation(() => ok({ worker_id: idle.id, request_id: "r1", models: null, warm: true }));
    wrap(<Workers admin />);
    fireEvent.click(await screen.findByRole("button", { name: "Prepare models" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/admin/gpu/workers/{worker_id}:prepare", {
        params: { path: { worker_id: idle.id } },
        body: { warm: true },
      }),
    );
  });
});
