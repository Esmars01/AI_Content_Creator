import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push }) }));
const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { VersionsPanel } = await import("./studio-panels");
const { ExportPanel } = await import("./export-panels");
const { VersionConsistency } = await import("./qc-panels");

type Init = { params?: { path?: Record<string, string> }; body?: Record<string, unknown> };
const ok = (data: unknown, status = 200) => Promise.resolve({ data, response: new Response(null, { status }) });

function wrap(children: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  );
}

describe("reachable backend actions (EXTRA)", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    push.mockReset();
  });

  it("duplicates the video shown and opens the copy (a)", async () => {
    get.mockImplementation(() => ok({ items: [] }));
    post.mockImplementation(() =>
      ok({ video_id: "vid-copy", version_id: "v-copy", job_id: "j1", state: "ready" }, 202),
    );
    const versions = [{ id: "v1", number: 1, state: "ready", origin: "plan", branch: "main", parent_version_id: null }];
    render(wrap(<VersionsPanel videoId="vid" versions={versions as never[]} currentId="v1" />));
    fireEvent.click(screen.getByRole("button", { name: "Duplicate video" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/videos/vid-copy?version=v-copy"));
    const [path, init] = post.mock.calls[0] as [string, Init];
    expect(path).toBe("/v1/videos/{video_id}:duplicate");
    expect(init.params?.path).toEqual({ video_id: "vid" });
    expect(init.body).toEqual({ version_id: "v1" });
  });

  it("renders for a platform whose presets have no render instead of only refusing (b)", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/v1/platforms")
        return ok([
          {
            id: "youtube",
            label: "YouTube",
            presets: [{ id: "youtube_1920x1080_30", aspect: "16:9", width: 1920, height: 1080, fps: 30 }],
            checklist: [],
          },
        ]);
      if (path === "/v1/versions/{version_id}/renders")
        return ok([
          { id: "r1", preset_id: "tiktok_1080x1920_30", is_proxy: false, status: "ready", provenance_mode: "real" },
        ]);
      return ok([]);
    });
    post.mockImplementation(() => ok({ job_id: "j-render", version_id: "v1" }, 202));
    render(wrap(<ExportPanel versionId="v1" versionState="ready" />));
    await screen.findByRole("option", { name: "YouTube" });
    fireEvent.change(screen.getByLabelText("Platform"), { target: { value: "youtube" } });
    fireEvent.click(await screen.findByRole("button", { name: "Render for YouTube" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, init] = post.mock.calls[0] as [string, Init];
    expect(path).toBe("/v1/versions/{version_id}/renders");
    expect(init.body).toEqual({ preset_ids: ["youtube_1920x1080_30"], proxy: false });
  });

  it("runs a consistency check when the version has no report yet (c)", async () => {
    // Regression: the panel rendered nothing without reports, so a check could never be started.
    get.mockImplementation(() => ok([]));
    post.mockImplementation(() => ok({ job_id: "j-cons", status: "queued" }, 202));
    render(wrap(<VersionConsistency versionId="v1" versionState="ready" />));
    fireEvent.click(await screen.findByRole("button", { name: "Run consistency check" }));
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/versions/{version_id}/consistency:run", {
        params: { path: { version_id: "v1" } },
      }),
    );
  });
});
