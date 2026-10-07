import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EditProposal } from "@/lib/edits";
import { keys } from "@/lib/queries";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn() }) }));

const post = vi.fn();
const get = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, POST: (...args: unknown[]) => post(...args), GET: (...args: unknown[]) => get(...args) },
  };
});

const { EditPanel, INSTRUCTION_MAX, ProposalCard } = await import("./edit-panel");
const { useStudio } = await import("@/lib/store");

const PROPOSAL: EditProposal = {
  id: "p1",
  version_id: "v1",
  job_id: "j1",
  instruction: "make him more skeptical",
  selection: { kind: "edit", editor: { scene_keys: ["scn_reveal"] } },
  status: "proposed",
  ops: [
    {
      op: "set_acting",
      scope: { scene_keys: ["scn_reveal"] },
      changes: { emotion: { displayed: { label: "skeptical", intensity_delta: 0.2 } } },
      reason: "displayed skepticism up",
    },
    { op: "add_behavior_event", scope: { scene_keys: ["scn_reveal"] }, event: { type: "eyebrow_raise" } },
  ],
  patch: [],
  impact: {
    regenerate: ["behavior.resolve:scn_reveal", "avatar.render:sht_4:c1:t1"],
    cascade: ["mix.audio:main"],
    keep: ["tts.segment:seg_1"],
    no_visible_effect: [
      { node_key: "post.expression:sht_4", reason: "the change leaves the compiled output unchanged" },
    ],
    generation: ["avatar.render:sht_4:c1:t1"],
    locks_blocking: [{ group: "voice", reason: "delivered audio kept for unchanged text (seg_2)" }],
    estimate: { usd: 0.02 },
    diff: [
      {
        path: "/scenes[scn_reveal]/acting/states[st_2]/emotion/displayed/label",
        kind: "changed",
        area: "acting",
        before: "serious",
        after: "skeptical",
      },
    ],
    planner: "fixture",
    assumptions: ["Applied to the selected scene"],
    issues: [],
  },
  coverage_delta: {
    items: [
      {
        item_ref: "/scenes[scn_reveal]/acting/states[st_2]/strategies/prosody",
        dimension: "prosody_rate",
        before: { level: "HONORED", method: "native_parametric" },
        after: { level: "HONORED", method: "native_parametric" },
        change: "request_changed",
        blocked_by_lock: "voice",
        reason: "the voice lock keeps the delivered audio",
      },
    ],
  },
  alternatives: [
    { strategy: "full_reperformance", estimate: { usd: 0.02 } },
    { strategy: "editorial_only", estimate: { usd: 0.001 } },
  ],
  result_version_id: null,
  created_at: "2026-10-04T08:00:00Z",
};

function withData(proposal: EditProposal, children: ReactNode, videoId = "vid") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(keys.edit(proposal.id), proposal);
  // the version the proposal was made on, and so the video Apply opens
  client.setQueryData(keys.version(proposal.version_id), { id: proposal.version_id, video_id: videoId });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("ProposalCard", () => {
  beforeEach(() => {
    replace.mockReset();
    post.mockReset();
    get.mockReset();
  });

  it("shows operations, changes, impact, coverage delta, alternatives and cost before anything changes", () => {
    render(withData(PROPOSAL, <ProposalCard proposalId="p1" />));
    expect(screen.getByTestId("proposal-status").textContent).toBe("proposed");
    const ops = screen.getByTestId("proposal-ops");
    expect(ops.textContent).toContain("Acting in scn_reveal: displayed skeptical, intensity +0.2");
    expect(ops.textContent).toContain("Add Eyebrow raise");
    expect(screen.getByLabelText("Changes").textContent).toContain("serious → skeptical");
    const impact = screen.getByTestId("impact-summary").textContent ?? "";
    expect(impact).toMatch(/2 regenerate/);
    expect(impact).toMatch(/1 keep/);
    expect(impact).toMatch(/1 no visible effect/);
    expect(impact).toContain("$0.02");
    expect(screen.getByLabelText("Held by locks").textContent).toContain("Voice");
    expect(screen.getByLabelText("Coverage delta").textContent).toContain("the voice lock keeps the delivered audio");
    expect(screen.getByRole("radio", { name: /Editorial only/ })).toBeTruthy();
    expect(post).not.toHaveBeenCalled(); // nothing changes until Apply
  });

  it("applies with the chosen alternative and opens the new version", async () => {
    post.mockResolvedValue({
      data: { job_id: "j2", new_version_id: "v2" },
      response: new Response(null, { status: 202 }),
    });
    render(withData(PROPOSAL, <ProposalCard proposalId="p1" />));
    fireEvent.click(screen.getByRole("radio", { name: /Editorial only/ }));
    fireEvent.click(screen.getByTestId("apply-edit"));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/videos/vid?version=v2"));
    const [path, init] = post.mock.calls[0] as [string, { body: { alternative: string | null }; headers: Json }];
    expect(path).toBe("/v1/edits/{edit_proposal_id}:apply");
    expect(init.body).toEqual({ alternative: "editorial_only" });
    expect(init.headers).toHaveProperty("Idempotency-Key");
  });

  it("explains a failed proposal and offers no Apply", () => {
    const failed: EditProposal = {
      ...PROPOSAL,
      status: "failed",
      impact: {
        issues: [
          {
            code: "locked",
            message: "Changing this needs the `camera` lock removed",
            path: "/scenes[scn_hook]/shots[sht_1]/camera",
          },
        ],
      },
      alternatives: [],
    };
    const close = vi.fn();
    render(withData(failed, <ProposalCard proposalId="p1" onClose={close} />));
    expect(screen.getByLabelText("Issues").textContent).toContain("`camera` lock");
    expect(screen.queryByTestId("apply-edit")).toBeNull();
    // a failed proposal changed nothing: nothing to reject (FAILED-PROPOSAL), only Close
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("applies onto the proposal's own video, not the page it is shown on (D1)", async () => {
    post.mockResolvedValue({
      data: { job_id: "j2", new_version_id: "v2" },
      response: new Response(null, { status: 202 }),
    });
    render(withData(PROPOSAL, <ProposalCard proposalId="p1" />, "video-a"));
    fireEvent.click(screen.getByTestId("apply-edit"));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/videos/video-a?version=v2"));
  });
});

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
const notFound = () =>
  Promise.resolve({
    error: { status: 404, title: "Not Found", detail: "not found" },
    response: new Response(null, { status: 404 }),
  });

/** GET answers by path: a version's edit history, a proposal, a version. */
function serve(versions: Record<string, { id: string; video_id: string }>) {
  get.mockImplementation((path: string, init?: { params?: { path?: Record<string, string> } }) => {
    const ids = init?.params?.path ?? {};
    if (path === "/v1/versions/{version_id}/edits") return ok([]);
    if (path === "/v1/edits/{edit_proposal_id}") return ok({ ...PROPOSAL, id: ids.edit_proposal_id });
    if (path === "/v1/versions/{version_id}") {
      const version = versions[ids.version_id ?? ""];
      return version ? ok(version) : notFound();
    }
    return notFound();
  });
}

describe("EditPanel", () => {
  beforeEach(() => {
    replace.mockReset();
    post.mockReset();
    get.mockReset();
    useStudio.setState({ activeProposals: {} });
  });

  it("keeps a proposal on the version it was made on: another video's Studio never shows it (D1)", async () => {
    // Regression: the store held one global proposal, so after a client-side navigation video B's
    // edit panel showed (and applied) the proposal made on video A.
    serve({ v1: { id: "v1", video_id: "video-a" }, vB: { id: "vB", video_id: "video-b" } });
    post.mockResolvedValue({ data: { edit_proposal_id: "p1", job_id: "j1" }, response: new Response() });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const panel = (versionId: string) => (
      <QueryClientProvider client={client}>
        <EditPanel key={versionId} versionId={versionId} sceneKeys={[]} />
      </QueryClientProvider>
    );
    const { rerender } = render(panel("v1"));
    fireEvent.change(screen.getByLabelText("Instruction"), { target: { value: "make him more skeptical" } });
    fireEvent.click(screen.getByTestId("propose-edit"));
    expect(await screen.findByTestId("proposal-card")).toBeTruthy();
    rerender(panel("vB"));
    await waitFor(() => expect(screen.queryByTestId("proposal-card")).toBeNull());
    rerender(panel("v1"));
    expect(await screen.findByTestId("proposal-card")).toBeTruthy();
  });

  it("refuses a half-filled or inverted time range instead of editing the whole video (D16)", async () => {
    serve({});
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <EditPanel versionId="v1" sceneKeys={[]} />
      </QueryClientProvider>,
    );
    const propose = screen.getByTestId("propose-edit") as HTMLButtonElement;
    fireEvent.change(screen.getByLabelText("Instruction"), { target: { value: "slower here" } });
    expect(propose.disabled).toBe(false);
    fireEvent.change(screen.getByLabelText("Range start (seconds)"), { target: { value: "3" } });
    expect(screen.getByTestId("range-error").textContent).toBe("Enter both a start and an end, or leave both empty.");
    expect(propose.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Range end (seconds)"), { target: { value: "2" } });
    expect(screen.getByTestId("range-error").textContent).toBe("The end must be after the start.");
    expect(propose.disabled).toBe(true);
    fireEvent.submit(propose.closest("form") as HTMLFormElement);
    expect(post).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Range end (seconds)"), { target: { value: "5.5" } });
    expect(screen.queryByTestId("range-error")).toBeNull();
    expect(propose.disabled).toBe(false);
    post.mockResolvedValue({ data: { edit_proposal_id: "p9", job_id: "j9" }, response: new Response() });
    fireEvent.click(propose);
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [, init] = post.mock.calls[0] as [string, { body: { selection: unknown } }];
    expect(init.body.selection).toEqual({ time_range_s: [3, 5.5] });
  });

  it("holds the instruction to the API's limit and counts near it (BREAK-LONG)", () => {
    // Regression: a 9,600-character instruction was accepted and the API answered with a schema error.
    serve({});
    render(
      <QueryClientProvider client={new QueryClient()}>
        <EditPanel versionId="v1" sceneKeys={[]} />
      </QueryClientProvider>,
    );
    const field = screen.getByLabelText("Instruction") as HTMLTextAreaElement;
    expect(field.maxLength).toBe(2000);
    expect(INSTRUCTION_MAX).toBe(2000);
    expect(screen.queryByTestId("instruction-count")).toBeNull();
    fireEvent.change(field, { target: { value: "x".repeat(1900) } });
    expect(screen.getByTestId("instruction-count").textContent).toBe("1900 / 2000 characters");
  });
});

type Json = Record<string, unknown>;
