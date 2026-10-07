import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { CancelJobButton } = await import("./cancel-job");
const { default: JobPage } = await import("@/app/(app)/jobs/[jobId]/page");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
// an already-settled promise, so `use(params)` reads it without suspending
const resolved = <T,>(value: T) => Object.assign(Promise.resolve(value), { status: "fulfilled", value });
const JOB = {
  id: "j1",
  kind: "generate",
  status: "running",
  progress: 0.64,
  target_type: "video_version",
  target_id: "v1",
  video_version_id: "v1",
  error: null,
  nodes: [],
};

describe("Cancel job (JOB-CANCEL)", () => {
  let confirm: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((path: string) => (path === "/v1/jobs/{job_id}" ? ok(JOB) : ok(null)));
    confirm = vi.spyOn(window, "confirm");
  });
  afterEach(() => confirm.mockRestore());

  it("asks first, cancels, then refreshes the job and the version", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const spy = vi.spyOn(client, "invalidateQueries");
    let resolve: (value: unknown) => void = () => undefined;
    post.mockImplementation(() => new Promise((r) => (resolve = r)));
    render(
      <QueryClientProvider client={client}>
        <CancelJobButton job={JOB} />
      </QueryClientProvider>,
    );
    const button = screen.getByRole("button", { name: "Cancel" }) as HTMLButtonElement;
    confirm.mockReturnValueOnce(false);
    fireEvent.click(button);
    expect(confirm).toHaveBeenCalledWith("Cancel this job? Work already done is kept.");
    expect(post).not.toHaveBeenCalled();
    confirm.mockReturnValueOnce(true);
    fireEvent.click(button);
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith("/v1/jobs/{job_id}:cancel", { params: { path: { job_id: "j1" } } }),
    );
    expect((screen.getByTestId("cancel-job") as HTMLButtonElement).disabled).toBe(true); // while pending
    resolve({ data: { job_id: "j1", status: "cancelling" }, response: new Response(null, { status: 202 }) });
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: ["version", "v1"] }));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["jobs"] });
    expect(screen.getByTestId("cancel-job").textContent).toBe("Cancelling…");
  });

  it("is offered on the job page for a running job only", async () => {
    // Regression: no screen offered a way to cancel a running job.
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { unmount } = render(
      <QueryClientProvider client={client}>
        <Suspense fallback={null}>
          <JobPage params={resolved({ jobId: "j1" })} />
        </Suspense>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("button", { name: "Cancel" })).toBeTruthy();
    unmount();
    get.mockImplementation(() => ok({ ...JOB, status: "succeeded", progress: 1 }));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Suspense fallback={null}>
          <JobPage params={resolved({ jobId: "j1" })} />
        </Suspense>
      </QueryClientProvider>,
    );
    expect(await screen.findByLabelText("Job progress")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });
});
