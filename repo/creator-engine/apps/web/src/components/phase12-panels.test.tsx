import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      GET: (...args: unknown[]) => get(...args),
      POST: (...args: unknown[]) => post(...args),
      PATCH: vi.fn(),
    },
  };
});

const { ClaimLedger } = await import("./research-panels");
const { ExportPanel } = await import("./export-panels");

function ok(data: unknown) {
  return Promise.resolve({ data, response: new Response(null, { status: 200 }) });
}

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const CLAIM = {
  id: "c1",
  video_id: "v",
  claim_key: "clm_1",
  segment_key: "seg_1",
  text: "90% of projects stall.",
  verdict: "unsupported",
  confidence: 0,
  evidence: [],
  evidence_fact_ids: [],
  reasons: ["no evidence supports it"],
  closed_book: false,
  blocking: true,
  overridable: true,
  detected: true,
  first_version_id: "x",
  last_version_id: "x",
  override_by: null,
  override_reason: null,
  override_at: null,
  in_version: true,
};

describe("ClaimLedger", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("offers an override in open book and none in closed book", async () => {
    get.mockImplementation(() =>
      ok([CLAIM, { ...CLAIM, id: "c2", claim_key: "clm_2", closed_book: true, overridable: false }]),
    );
    post.mockImplementation(() => ok({ ...CLAIM, override_by: "u", override_reason: "Our survey." }));
    render(wrap(<ClaimLedger versionId="ver" />));
    expect(await screen.findByText(/2 unsupported/)).toBeTruthy();
    expect(screen.getByText(/Closed-book claims cannot be overridden/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Override" })).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("Override reason for clm_1"), { target: { value: "Our survey." } });
    fireEvent.click(screen.getByRole("button", { name: "Override" }));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(post.mock.calls[0]?.[0]).toBe("/v1/claims/{claim_id}:override");
    expect(post.mock.calls[0]?.[1]).toMatchObject({
      params: { path: { claim_id: "c1" } },
      body: { reason: "Our survey." },
    });
  });
});

describe("ExportPanel", () => {
  beforeEach(() => {
    get.mockReset();
    post.mockReset();
  });

  it("explains why a mock-provenance render cannot be exported", async () => {
    const answers: Record<string, unknown> = {
      "/v1/platforms": [
        {
          id: "tiktok",
          label: "TikTok",
          verified_at: null,
          presets: [{ id: "tiktok_1080x1920_30", aspect: "9:16", width: 1080, height: 1920, fps: 30 }],
          checklist: [{ key: "ai_generated_label", label: "AI label on", required: true }],
          limits: {},
          thumbnail: { width: 1080, height: 1920 },
        },
      ],
      "/v1/versions/{version_id}/renders": [
        {
          id: "r1",
          preset_id: "tiktok_1080x1920_30",
          aspect: "9:16",
          is_proxy: false,
          provenance_mode: "mock_dev",
          status: "ready",
        },
      ],
      "/v1/versions/{version_id}/packaging": [],
      "/v1/versions/{version_id}/captions": [],
      "/v1/versions/{version_id}/claims": [],
      "/v1/versions/{version_id}/exports": [],
    };
    get.mockImplementation((path: string) => ok(answers[path] ?? []));
    render(wrap(<ExportPanel versionId="ver" versionState="ready" />));
    await screen.findByRole("option", { name: "TikTok" });
    fireEvent.change(screen.getByLabelText("Platform"), { target: { value: "tiktok" } });
    await screen.findByText("AI label on");
    fireEvent.change(screen.getByLabelText("Render (preset)"), { target: { value: "r1" } });
    expect(await screen.findByText(/cannot be exported/)).toBeTruthy();
    expect(screen.getByText("Approve the packaging for this platform first.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Export" }) as HTMLButtonElement).disabled).toBe(true);
    expect(post).not.toHaveBeenCalled();
  });
});
