import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { default: TemplatesPage } = await import("./page");
const { BrandKitsManager } = await import("@/components/brand-kits");

type Init = { body?: Record<string, unknown> };
const ok = (data: unknown, status = 200) => Promise.resolve({ data, response: new Response(null, { status }) });
const TEMPLATE = {
  id: "t1",
  name: "Bold captions",
  kind: "caption",
  version: 1,
  latest: true,
  description: "",
  body: { captions: { style_id: "bold" } },
  effective: { captions: { style_id: "bold" } },
  composes_from: [],
  conflicts: [],
  created_at: "2026-10-07T10:00:00Z",
};

function wrap(children: ReactNode) {
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {children}
    </QueryClientProvider>
  );
}

const calls = (path: string, dry?: boolean) =>
  post.mock.calls.filter(([p, init]) => p === path && (dry === undefined || (init as Init).body?.preview === dry))
    .length;

describe("double submits (D13, D15)", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    get.mockImplementation((path: string) => {
      if (path === "/v1/spec-templates") return ok({ items: [TEMPLATE] });
      if (path === "/v1/spec-templates/{spec_template_id}") return ok(TEMPLATE);
      if (path === "/v1/brand-kits") return ok({ items: [] });
      return ok({ items: [] });
    });
  });

  it("Propose edit runs once per click burst and needs a new preview after (D13)", async () => {
    let resolve: (value: unknown) => void = () => undefined;
    post.mockImplementation((_path: string, init: Init) =>
      init.body?.preview
        ? ok({ operations: [{ op: "set_captions", style_id: "bold" }] })
        : new Promise((r) => (resolve = r)),
    );
    render(wrap(<TemplatesPage />));
    fireEvent.click(await screen.findByRole("button", { name: "Bold captions" }));
    fireEvent.change(await screen.findByLabelText("Apply to version (id)"), { target: { value: "v1" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    const propose = screen.getByRole("button", { name: "Propose edit" }) as HTMLButtonElement;
    await waitFor(() => expect(propose.disabled).toBe(false));
    fireEvent.click(propose);
    await act(() => new Promise((r) => setTimeout(r, 20))); // a person's second click comes later
    fireEvent.click(propose);
    await waitFor(() => expect(propose.disabled).toBe(true));
    expect(calls("/v1/spec-templates/{spec_template_id}:apply", false)).toBe(1);
    resolve({ data: { edit_proposal_id: "e1" }, response: new Response(null, { status: 202 }) });
    expect(await screen.findByText(/Proposed as an edit/)).toBeTruthy();
    fireEvent.click(propose);
    expect(calls("/v1/spec-templates/{spec_template_id}:apply", false)).toBe(1);
  });

  it("Save template resets the form after success, so a second click saves nothing (D15)", async () => {
    post.mockImplementation((_path: string, init: Init) => ok({ id: "t2", name: init.body?.name }, 201));
    render(wrap(<TemplatesPage />));
    fireEvent.change(await screen.findByLabelText("Name"), { target: { value: "From v1" } });
    fireEvent.change(screen.getByLabelText("Version id"), { target: { value: "v1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save template" }));
    expect(await screen.findByText("Template “From v1” saved.")).toBeTruthy();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Save template" }));
    expect(calls("/v1/spec-templates")).toBe(1);
  });

  it("Create brand kit resets the form after success (D15)", async () => {
    post.mockImplementation((_path: string, init: Init) => ok({ id: "k1", name: init.body?.name }, 201));
    render(wrap(<BrandKitsManager />));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Acme" } });
    fireEvent.click(screen.getByRole("button", { name: "Create brand kit" }));
    expect(await screen.findByText("Brand kit “Acme” created.")).toBeTruthy();
    expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Create brand kit" }));
    expect(calls("/v1/brand-kits")).toBe(1);
  });
});

describe("version picker (TPL-UUID)", () => {
  it("applies a template to a video picked by project and title, no UUID typed", async () => {
    // Regression: the only way to say which version was to paste its UUID.
    get.mockReset();
    post.mockReset();
    get.mockImplementation((path: string) => {
      if (path === "/v1/spec-templates") return ok({ items: [TEMPLATE] });
      if (path === "/v1/spec-templates/{spec_template_id}") return ok(TEMPLATE);
      if (path === "/v1/projects") return ok({ items: [{ id: "p1", name: "Launch" }] });
      if (path === "/v1/projects/{project_id}/videos")
        return ok({ items: [{ id: "vid1", title: "Sleep tips", current_version_id: "ver7" }] });
      return ok({ items: [] });
    });
    post.mockImplementation(() => ok({ operations: [{ op: "set_captions" }] }));
    render(wrap(<TemplatesPage />));
    fireEvent.click(await screen.findByRole("button", { name: "Bold captions" }));
    await screen.findByLabelText("Apply to version (id)");
    const project = document.getElementById("apply-project") as HTMLSelectElement;
    await screen.findAllByRole("option", { name: "Launch" });
    fireEvent.change(project, { target: { value: "p1" } });
    await screen.findAllByRole("option", { name: "Sleep tips" });
    const video = document.getElementById("apply-video") as HTMLSelectElement;
    fireEvent.change(video, { target: { value: "ver7" } });
    expect((screen.getByLabelText("Apply to version (id)") as HTMLInputElement).value).toBe("ver7");
    fireEvent.click(screen.getByRole("button", { name: "Preview" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect((post.mock.calls[0]?.[1] as Init).body?.version_id).toBe("ver7");
  });
});
