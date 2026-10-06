import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { PlatesPanel } = await import("./world-panels");

const ok = (data: unknown) => ({ data, response: new Response() });
const version = (status: string) => ({
  id: "wv1",
  number: 2,
  status,
  plate_candidates: {},
  plates: {},
  fingerprints_artifact_id: "a1",
});

describe("PlatesPanel", () => {
  it("shows the version as approved once approval succeeds", async () => {
    // Regression (audit 2026-10): approval refreshed only the world, never the version the panel
    // shows, so it stayed "Draft" with the Approve button. Studio jobs used to refetch in an endless
    // loop (W-FE2), which hid this; with that loop fixed, nothing refreshed the version any more.
    let status = "draft";
    get.mockImplementation(() => Promise.resolve(ok(version(status))));
    post.mockImplementation(() => {
      status = "approved";
      return Promise.resolve(ok({ id: "wv1", status }));
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const world = { id: "w1", current_version_id: "wv0", versions: [{ id: "wv1", number: 2, status: "draft" }] };
    const { findByRole, findByText } = render(
      <QueryClientProvider client={client}>
        <PlatesPanel world={world} />
      </QueryClientProvider>,
    );
    fireEvent.click(await findByRole("button", { name: "Approve world version" }));
    expect(await findByText(/Approved: plates are frozen\./)).toBeTruthy();
    expect(post).toHaveBeenCalledWith("/v1/world-versions/{world_version_id}:approve", {
      params: { path: { world_version_id: "wv1" } },
    });
  });
});
