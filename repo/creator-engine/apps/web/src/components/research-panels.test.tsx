import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const { SourcesPanel } = await import("./research-panels");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

describe("SourcesPanel", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("clears the file input after a document is added (D12)", async () => {
    // Regression: the input kept showing the uploaded file while the form had forgotten it, so the
    // next Add failed with "choose a file".
    get.mockImplementation(() => ok({ items: [] }));
    post.mockImplementation((path: string) => {
      if (path === "/v1/assets:initiate-upload")
        return ok({
          asset_id: "a1",
          upload: { part_size: 1024, parts: [{ part_number: 1, url: "https://s3.test/a1", headers: {} }] },
        });
      return ok({ id: "s1" });
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 200 }))),
    );
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <SourcesPanel projectId="p1" />
      </QueryClientProvider>,
    );
    fireEvent.change(screen.getByLabelText("Source type"), { target: { value: "file" } });
    const input = screen.getByLabelText("Document") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["notes"], "notes.txt", { type: "text/plain" })] } });
    // (jsdom cannot set a file input's value, so its required check would block a click)
    fireEvent.submit(screen.getByRole("button", { name: "Add source" }).closest("form") as HTMLFormElement);
    await waitFor(() => expect(post).toHaveBeenCalledWith("/v1/projects/{project_id}/sources", expect.anything()));
    await waitFor(() => expect(screen.getByLabelText("Document")).not.toBe(input));
    expect((screen.getByLabelText("Document") as HTMLInputElement).files?.length ?? 0).toBe(0);
  });
});
