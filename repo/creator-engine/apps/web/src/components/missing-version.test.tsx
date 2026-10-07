import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { type ReactNode, Suspense } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let search = new URLSearchParams();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => search,
  usePathname: () => "/",
}));
const get = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, GET: (...args: unknown[]) => get(...args) } };
});

const { default: StudioPage } = await import("@/app/(app)/videos/[videoId]/page");
const { default: PrevizPage } = await import("@/app/(app)/videos/[videoId]/versions/[versionId]/previz/page");
const { default: ProjectPage } = await import("@/app/(app)/projects/[projectId]/page");
const { usePendingVersions } = await import("@/lib/store");

type Init = { params?: { path?: Record<string, string> } };
const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
const problem = (status: number) =>
  Promise.resolve({
    error: { status, title: status === 404 ? "Not Found" : "Unprocessable", detail: `${status}` },
    response: new Response(null, { status }),
  });
// an already-settled promise, so `use(params)` reads it without suspending
const resolved = <T,>(value: T) => Object.assign(Promise.resolve(value), { status: "fulfilled", value });

const VIDEO = { id: "vid", title: "My video", current_version_id: "v-current", planning: false };

function serve(extra: (path: string, ids: Record<string, string>) => Promise<unknown> | null = () => null) {
  get.mockImplementation((path: string, init?: Init) => {
    const ids = init?.params?.path ?? {};
    const answer = extra(path, ids);
    if (answer) return answer;
    if (path === "/v1/me") return ok({ role: "editor", user: { id: "u1" }, org: { id: "o1" }, memberships: [] });
    if (path === "/v1/videos/{video_id}") return ok(VIDEO);
    if (path === "/v1/videos/{video_id}/versions") return ok({ items: [] });
    if (path === "/v1/versions/{version_id}") return problem(ids.version_id === "not-a-uuid" ? 422 : 404);
    return problem(404);
  });
}

function page(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <Suspense fallback={null}>{children}</Suspense>
    </QueryClientProvider>
  );
}

/** Lets the 2 s version polls run (fake timers). */
async function polls(n: number) {
  for (let i = 0; i < n; i++) await act(() => vi.advanceTimersByTimeAsync(2_100));
}

describe("broken deep links (NAV-D17)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    get.mockReset();
    usePendingVersions.setState({ jobs: {} });
  });
  afterEach(() => vi.useRealTimers());

  it("a version id that does not exist ends in 'This version does not exist', not a wait forever", async () => {
    // Regression: the Studio showed "Creating the new version…" forever for any 404.
    search = new URLSearchParams({ version: "0192f0a0-0000-7000-8000-00000000dead" });
    serve();
    render(page(<StudioPage params={resolved({ videoId: "vid" })} />));
    await polls(6);
    expect((await screen.findByTestId("version-missing")).textContent).toBe("This version does not exist.");
    expect(screen.getByRole("link", { name: "Open the video's current version" }).getAttribute("href")).toBe(
      "/videos/vid",
    );
    expect(screen.queryByText("Creating the new version…")).toBeNull();
  });

  it("an id that is not a UUID (422) is missing at once, not a blank skeleton", async () => {
    search = new URLSearchParams({ version: "not-a-uuid" });
    serve();
    render(page(<StudioPage params={resolved({ videoId: "vid" })} />));
    expect(await screen.findByTestId("version-missing")).toBeTruthy();
  });

  it("keeps 'Creating the new version…' while this tab's apply job runs", async () => {
    search = new URLSearchParams({ version: "v-new" });
    usePendingVersions.getState().expectVersion("v-new", "j-apply");
    serve((path) => (path === "/v1/jobs/{job_id}" ? ok({ id: "j-apply", status: "running", progress: 0.2 }) : null));
    render(page(<StudioPage params={resolved({ videoId: "vid" })} />));
    await polls(7);
    expect(screen.getByText("Creating the new version…")).toBeTruthy();
    expect(screen.queryByTestId("version-missing")).toBeNull();
  });

  it("the previz page of a missing version says so instead of 'Planning…' forever", async () => {
    search = new URLSearchParams();
    serve();
    render(
      page(<PrevizPage params={resolved({ videoId: "vid", versionId: "0192f0a0-0000-7000-8000-00000000dead" })} />),
    );
    await polls(6);
    expect(await screen.findByTestId("version-missing")).toBeTruthy();
    expect(screen.queryByText("This page updates by itself.")).toBeNull();
  });

  it("a missing project is 'This project does not exist', not the project page shell", async () => {
    serve();
    render(page(<ProjectPage params={resolved({ projectId: "0192f0a0-0000-7000-8000-00000000dead" })} />));
    expect(await screen.findByText("This project does not exist.")).toBeTruthy();
    expect(screen.queryByRole("link", { name: "New video" })).toBeNull();
  });

  it("a transient failure is an error with a retry, not 'does not exist'", async () => {
    serve((path) =>
      path === "/v1/projects/{project_id}"
        ? Promise.resolve({
            error: { status: 502, title: "Bad Gateway" },
            response: new Response(null, { status: 502 }),
          })
        : null,
    );
    render(page(<ProjectPage params={resolved({ projectId: "p1" })} />));
    await polls(3); // transient errors are retried first
    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    expect(screen.queryByText("This project does not exist.")).toBeNull();
  });

  it("a plan job that failed shows why on the previz page, not 'This version does not exist'", async () => {
    // Regression (product audit): a creator whose designed voice had no transcript failed to plan in
    // ~14 s; the previz page, after five 404s, said the version did not exist instead of the reason.
    search = new URLSearchParams({ job: "plan-job" });
    serve((path) =>
      path === "/v1/jobs/{job_id}"
        ? ok({
            id: "plan-job",
            kind: "plan",
            status: "failed",
            progress: 1,
            error: { message: "the voice is not usable" },
          })
        : null,
    );
    render(
      page(<PrevizPage params={resolved({ videoId: "vid", versionId: "0192f0a0-0000-7000-8000-00000000beef" })} />),
    );
    await polls(7);
    expect(await screen.findByText("Planning failed: the voice is not usable")).toBeTruthy();
    expect(screen.queryByTestId("version-missing")).toBeNull();
    expect(screen.getByRole("link", { name: "Plan a video again" }).getAttribute("href")).toBe("/create");
  });

  it("an edit whose job failed says the change could not be applied, in the Studio", async () => {
    search = new URLSearchParams({ version: "0192f0a0-0000-7000-8000-00000000cafe" });
    usePendingVersions.setState({ jobs: { "0192f0a0-0000-7000-8000-00000000cafe": "apply-job" } });
    serve((path) =>
      path === "/v1/jobs/{job_id}" ? ok({ id: "apply-job", kind: "edit_apply", status: "failed", progress: 1 }) : null,
    );
    render(page(<StudioPage params={resolved({ videoId: "vid" })} />));
    await polls(7);
    expect(await screen.findByText(/This change could not be applied: its job failed or was cancelled\./)).toBeTruthy();
    expect(screen.queryByTestId("version-missing")).toBeNull();
    expect(screen.queryByText("Creating the new version…")).toBeNull();
  });

  it("a video whose planning failed offers to plan again, not only 'No version yet' (PLAN-FAIL)", async () => {
    // Regression: a plan that failed leaves the video without a version; its Studio was a dead end.
    search = new URLSearchParams();
    serve((path) =>
      path === "/v1/videos/{video_id}" ? ok({ ...VIDEO, current_version_id: null, planning: false }) : null,
    );
    render(page(<StudioPage params={resolved({ videoId: "vid" })} />));
    expect((await screen.findByTestId("plan-again")).getAttribute("href")).toBe("/create");
    expect(screen.getByText(/its planning did not finish/)).toBeTruthy();
  });
});
