import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn() }) }));
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

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
const version = (id: string, number: number, state: string, origin = "plan") => ({
  id,
  number,
  state,
  origin,
  branch: "main",
  parent_version_id: null,
  flags: [],
  frozen_at: null,
});

function wrap(children: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  );
}

const row = (label: string) => screen.getByText(label).closest("li") as HTMLElement;

describe("VersionsPanel", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    replace.mockReset();
  });

  it("offers Resume only for versions that started generating; the others link to their previz (D5)", async () => {
    // Regression: Resume was offered for a failed version that never reached approval, and the API
    // refused ("this version never started generating: replan or approve it instead").
    get.mockImplementation((path: string, init?: { params?: { query?: Record<string, unknown> } }) => {
      if (path === "/v1/jobs" && init?.params?.query?.kind === "generate")
        return ok({ items: [{ id: "j2", kind: "generate", video_version_id: "v2" }], next_cursor: null });
      return ok({ items: [] });
    });
    const versions = [version("v1", 1, "failed"), version("v2", 2, "failed"), version("v3", 3, "partial", "edit")];
    render(wrap(<VersionsPanel videoId="vid" versions={versions as never[]} currentId="v3" />));
    expect(await within(row("v2 · Plan")).findByRole("button", { name: "Resume" })).toBeTruthy();
    expect(within(row("v3 · Edit")).getByRole("button", { name: "Resume" })).toBeTruthy();
    const never = row("v1 · Plan");
    expect(within(never).queryByRole("button", { name: "Resume" })).toBeNull();
    expect(within(never).getByRole("link", { name: "Previz" }).getAttribute("href")).toBe(
      "/videos/vid/versions/v1/previz",
    );
  });
});
