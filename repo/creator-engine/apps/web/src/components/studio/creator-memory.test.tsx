import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
const get = vi.fn();
const post = vi.fn();
const del = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      GET: (...args: unknown[]) => get(...args),
      POST: (...args: unknown[]) => post(...args),
      DELETE: (...args: unknown[]) => del(...args),
      PATCH: vi.fn(),
    },
  };
});

const { MemoryPanel } = await import("./creator-panels");

const ok = (data: unknown, status = 200) => Promise.resolve({ data, response: new Response(null, { status }) });
const ITEM = {
  id: "m1",
  category: "persona_fact",
  kind: "persona_fact",
  status: "active",
  pinned: false,
  conflict_state: "none",
  conflict_ids: [],
  text: "Alex owns a grey cat",
  confidence: 1,
  source: { kind: "authored" },
  evidence_count: 0,
  last_seen_at: "2026-10-07T10:00:00Z",
  embedding_model: null,
  creator_version_from: null,
  creator_version_to: null,
};

function wrap(children: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  );
}

describe("Creator memory (MEM-CRUD)", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    del.mockReset();
  });

  it("authors a fact from the empty memory", async () => {
    // Regression: the panel said items are "authored" but offered no way to author one.
    get.mockImplementation((path: string) => {
      if (path === "/v1/me") return ok({ role: "editor", user: { id: "u1" }, org: { id: "o1" }, memberships: [] });
      if (path === "/v1/creators/{creator_id}/memory") return ok({ items: [] });
      return ok({ id: "c1", versions: [] });
    });
    post.mockImplementation(() => ok(ITEM, 201));
    render(wrap(<MemoryPanel creatorId="c1" />));
    fireEvent.change(await screen.findByLabelText("Subject"), { target: { value: "Alex" } });
    fireEvent.change(screen.getByLabelText("Predicate"), { target: { value: "owns" } });
    const add = screen.getByRole("button", { name: "Add" }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Object"), { target: { value: "a grey cat" } });
    await waitFor(() => expect(add.disabled).toBe(false));
    fireEvent.click(add);
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, init] = post.mock.calls[0] as [string, { params: unknown; body: Record<string, unknown> }];
    expect(path).toBe("/v1/creators/{creator_id}/memory");
    expect(init.body).toEqual({
      kind: "persona_fact",
      value: { subject: "Alex", predicate: "owns", object: "a grey cat" },
      text: "Alex owns a grey cat",
    });
  });

  it("deletes an item after confirming", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/v1/me") return ok({ role: "editor", user: { id: "u1" }, org: { id: "o1" }, memberships: [] });
      if (path === "/v1/creators/{creator_id}/memory") return ok({ items: [ITEM] });
      return ok({ id: "c1", versions: [] });
    });
    del.mockImplementation(() => ok({ job_id: "j1", memory_item_id: "m1" }, 202));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(wrap(<MemoryPanel creatorId="c1" />));
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    expect(confirm).toHaveBeenCalledWith("Delete “Alex owns a grey cat” from memory?");
    await waitFor(() => expect(del).toHaveBeenCalled());
    expect(del.mock.calls[0]?.[1]).toEqual({ params: { path: { memory_item_id: "m1" } } });
    confirm.mockRestore();
  });
});
