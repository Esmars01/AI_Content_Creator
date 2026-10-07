import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/create",
}));
const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { default: CreatePage } = await import("./page");

type Init = {
  params?: { path?: Record<string, string>; query?: Record<string, unknown> };
  body?: Record<string, unknown>;
};
const ok = (data: unknown, status = 200) => Promise.resolve({ data, response: new Response(null, { status }) });
const refused = () =>
  Promise.resolve({
    error: { status: 422, title: "Unprocessable", detail: "the plan was refused" },
    response: new Response(null, { status: 422 }),
  });

const OPTIONS = {
  input_modes: ["auto", "exact_script"],
  quality_tiers: ["draft", "final"],
  modes: [],
  platforms: [],
  aspects: ["9:16"],
  camera_profiles: [],
  caption_styles: [],
  music_moods: [],
  languages: [],
  strategy_packs: [],
  routing_profiles: [],
  default_mode: "explainer",
};

function serve(projects: { id: string; name: string }[]) {
  get.mockImplementation((path: string, init?: Init) => {
    if (path === "/v1/me") return ok({ role: "editor", user: { id: "u1" }, org: { id: "o1" }, memberships: [] });
    if (path === "/v1/create-options") return ok(OPTIONS);
    if (path === "/v1/projects") return ok({ items: projects });
    if (path === "/v1/creators")
      return ok({
        items: [
          { id: "c1", name: "Ada", status: "active", current_version_id: "cv1" },
          { id: "c2", name: "Bo", status: "active", current_version_id: "cv2" },
        ],
      });
    if (path === "/v1/creators/{creator_id}") return ok({ id: init?.params?.path?.creator_id, current_version: null });
    if (path === "/v1/voices")
      return ok(
        init?.params?.query?.creator_id === "c1"
          ? [{ id: "voice1", name: "Ada's voice", current_version_id: "vv1" }]
          : [],
      );
    if (path === "/v1/projects/{project_id}/sources")
      return ok({
        items:
          init?.params?.path?.project_id === "p1"
            ? [{ id: "s1", status: "ingested", title: "Report", trust: "user_provided", fact_count: 3, kind: "pdf" }]
            : [],
      });
    return ok({ items: [] });
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <CreatePage />
    </QueryClientProvider>,
  );
}

const step = (name: string) => fireEvent.click(screen.getByRole("button", { name: new RegExp(`^\\d+${name}$`) }));
const videoBodies = () =>
  post.mock.calls.filter(([path]) => path === "/v1/projects/{project_id}/videos").map(([, init]) => init as Init);

describe("Create", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    push.mockReset();
  });

  it("creates the default project once, however often Plan is retried (D6)", async () => {
    // Regression: with no project, every retry after a failed plan created another "My videos".
    serve([]);
    post.mockImplementation((path: string) =>
      path === "/v1/projects" ? ok({ id: "p-new", name: "My videos" }, 201) : refused(),
    );
    renderPage();
    fireEvent.change(await screen.findByLabelText("Idea, outline, notes or exact script"), {
      target: { value: "Why agents fail" },
    });
    step("Plan");
    fireEvent.click(screen.getByRole("button", { name: "Plan video" }));
    expect(await screen.findByText("the plan was refused")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Plan video" }));
    await waitFor(() => expect(videoBodies()).toHaveLength(2));
    expect(post.mock.calls.filter(([path]) => path === "/v1/projects")).toHaveLength(1);
    expect(videoBodies().map((init) => init.params?.path?.project_id)).toEqual(["p-new", "p-new"]);
  });

  it("drops the voice of the previous creator and the sources of the previous project (D11)", async () => {
    serve([
      { id: "p1", name: "Research" },
      { id: "p2", name: "Other" },
    ]);
    post.mockImplementation(() => refused());
    renderPage();
    fireEvent.change(await screen.findByLabelText("Idea, outline, notes or exact script"), {
      target: { value: "Why agents fail" },
    });
    await waitFor(() => expect((screen.getByLabelText("Project") as HTMLSelectElement).value).toBe("p1"));
    step("Advanced");
    fireEvent.click(await screen.findByRole("checkbox", { name: /Report/ }));
    step("Creator");
    fireEvent.change(await screen.findByLabelText("Creator"), { target: { value: "c1" } });
    step("Voice");
    await screen.findByRole("option", { name: "Ada's voice" });
    fireEvent.change(screen.getByLabelText("Voice"), { target: { value: "vv1" } });
    step("Creator");
    fireEvent.change(screen.getByLabelText("Creator"), { target: { value: "c2" } });
    step("Idea or script");
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "p2" } });
    step("Plan");
    fireEvent.click(screen.getByRole("button", { name: "Plan video" }));
    await waitFor(() => expect(videoBodies()).toHaveLength(1));
    const body = videoBodies()[0]?.body as {
      cast: { creator_id: string; voice_version_id: string | null }[];
      sources: string[];
    };
    expect(body.cast).toEqual([{ creator_id: "c2", role: "host", voice_version_id: null }]);
    expect(body.sources).toEqual([]);
  });
});
