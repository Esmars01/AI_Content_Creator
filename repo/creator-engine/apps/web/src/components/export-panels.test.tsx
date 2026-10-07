import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
const patch = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      GET: (...args: unknown[]) => get(...args),
      POST: (...args: unknown[]) => post(...args),
      PATCH: (...args: unknown[]) => patch(...args),
    },
  };
});
const startDownload = vi.fn();
vi.mock("@/lib/utils", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/utils")>();
  return { ...actual, startDownload: (...args: unknown[]) => startDownload(...args) };
});

const { CaptionsPanel, PackagingPanel } = await import("./export-panels");

type Json = Record<string, unknown>;
type Init = { params?: { path?: Record<string, string> }; body?: Json };
const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

const ROW = {
  id: "pk1",
  version_id: "v1",
  platform: "tiktok",
  title: "Stored title",
  description: "Stored description",
  hashtags: ["ai"],
  cta_text: "Follow",
  thumbnail_artifact_ids: ["a1"],
  thumbnail_candidates: [
    { artifact_id: "a1", text: "first", at_s: 1 },
    { artifact_id: "a2", text: "second", at_s: 2 },
  ],
  limits: { title_max_chars: 100, description_max_chars: 2000, hashtags_max: 5, cta_max_chars: 80, sources: {} },
  issues: [],
  generator: { kind: "llm" },
  status: "draft",
  approved_by: null,
  approved_at: null,
  job_id: null,
  updated_at: "2026-10-07T10:00:00Z",
};

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("PackagingPanel", () => {
  let row: Json;
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
    patch.mockReset();
    row = { ...ROW };
    get.mockImplementation((path: string) => {
      if (path === "/v1/platforms") return ok([]);
      if (path === "/v1/versions/{version_id}/packaging") return ok([row]);
      return ok(null);
    });
    // the API stores what is sent and bumps updated_at (the editor remounts on it)
    patch.mockImplementation((_path: string, init: Init) => {
      const body = init.body ?? {};
      row = {
        ...row,
        ...Object.fromEntries(Object.entries(body).filter(([k]) => k !== "thumbnail_artifact_id")),
        ...(body.thumbnail_artifact_id ? { thumbnail_artifact_ids: [body.thumbnail_artifact_id] } : {}),
        status: "draft",
        updated_at: `2026-10-07T10:0${patch.mock.calls.length}:00Z`,
      };
      return ok(row);
    });
  });

  it("keeps unsaved text when a thumbnail is chosen (D7)", async () => {
    // Regression: choosing a thumbnail saved only the thumbnail and the editor remounted on the new
    // updated_at, discarding the unsaved title.
    render(wrap(<PackagingPanel versionId="v1" targets={[]} />));
    const title = (await screen.findByLabelText("Title")) as HTMLInputElement;
    fireEvent.change(title, { target: { value: "My new title" } });
    fireEvent.click(screen.getAllByRole("radio")[1] as HTMLElement);
    await waitFor(() => expect(patch).toHaveBeenCalledOnce());
    const [, init] = patch.mock.calls[0] as [string, Init];
    expect(init.body).toMatchObject({ title: "My new title", thumbnail_artifact_id: "a2" });
    await waitFor(() => expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("My new title"));
  });

  it("approves what is on screen: unsaved edits are saved first (D8)", async () => {
    // Regression: Approve approved the stored copy and ignored the edited description.
    post.mockImplementation(() => ok({ ...row, status: "approved" }));
    render(wrap(<PackagingPanel versionId="v1" targets={[]} />));
    fireEvent.change(await screen.findByLabelText("Description"), { target: { value: "Edited description" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and approve" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(patch).toHaveBeenCalledOnce();
    const [, init] = patch.mock.calls[0] as [string, Init];
    expect(init.body).toMatchObject({ description: "Edited description", title: "Stored title" });
    expect(patch.mock.invocationCallOrder[0]).toBeLessThan(post.mock.invocationCallOrder[0] as number);
    expect(post.mock.calls[0]?.[0]).toBe("/v1/packaging/{packaging_id}:approve");
  });

  it("approves directly when nothing changed", async () => {
    post.mockImplementation(() => ok({ ...row, status: "approved" }));
    render(wrap(<PackagingPanel versionId="v1" targets={[]} />));
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(post).toHaveBeenCalledOnce());
    expect(patch).not.toHaveBeenCalled();
  });
});

describe("CaptionsPanel", () => {
  beforeEach(() => {
    get.mockReset();
    startDownload.mockReset();
  });

  it("offers every caption file as a download, translations included (DL-02)", async () => {
    // Regression: languages and formats were plain text; the files could never be fetched.
    const caption = (id: string, language: string, format: string, review_state: string) => ({
      id,
      version_id: "v1",
      language,
      style_id: "default",
      format,
      review_state,
      artifact_id: `art-${id}`,
      created_at: "2026-10-07T10:00:00Z",
    });
    get.mockImplementation((path: string, init: Init) => {
      if (path === "/v1/versions/{version_id}/captions")
        return ok([
          caption("c1", "en", "srt", "n/a"),
          caption("c2", "en", "vtt", "n/a"),
          caption("c3", "de", "ass", "approved"),
        ]);
      if (path === "/v1/create-options") return ok({ languages: [] });
      if (path === "/v1/captions/{caption_id}/download") {
        const id = init.params?.path?.caption_id;
        return ok({ url: `https://s3.test/${id}`, filename: `captions.${id}`, expires_at: "2026-10-07T11:00:00Z" });
      }
      return ok(null);
    });
    render(wrap(<CaptionsPanel versionId="v1" videoId="vid" />));
    fireEvent.click(await screen.findByRole("button", { name: "Download de ass captions" }));
    await waitFor(() => expect(startDownload).toHaveBeenCalledWith("https://s3.test/c3", "captions.c3"));
    expect(get).toHaveBeenCalledWith("/v1/captions/{caption_id}/download", { params: { path: { caption_id: "c3" } } });
    expect(screen.getByRole("button", { name: "Download en srt captions" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Download en vtt captions" })).toBeTruthy();
  });
});
