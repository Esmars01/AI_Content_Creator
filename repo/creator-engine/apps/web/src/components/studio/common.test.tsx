import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";

const get = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, GET: (...args: unknown[]) => get(...args) } };
});

const { useStudioJob } = await import("./common");

function Panel({ onRender }: { onRender: () => void }) {
  // like every caller: a fresh array literal on each render
  const job = useStudioJob([["thing", "t1"]]);
  onRender();
  useEffect(() => job.setJobId("j1"), []); // eslint-disable-line react-hooks/exhaustive-deps
  return <p>{job.status}</p>;
}

describe("useStudioJob", () => {
  it("invalidates its keys once when the job finishes, not after every render", async () => {
    // Regression: the effect depended on the inline array, so once the job was terminal every
    // render invalidated again, and every refetch re-rendered: an endless refetch loop.
    get.mockResolvedValue({ data: { id: "j1", status: "succeeded" }, response: new Response() });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");
    let renders = 0;
    const { findByText, rerender } = render(
      <QueryClientProvider client={client}>
        <Panel onRender={() => renders++} />
      </QueryClientProvider>,
    );
    await findByText("succeeded");
    for (let i = 0; i < 5; i++) {
      rerender(
        <QueryClientProvider client={client}>
          <Panel onRender={() => renders++} />
        </QueryClientProvider>,
      );
    }
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["thing", "t1"] });
    expect(renders).toBeGreaterThan(5);
  });
});
