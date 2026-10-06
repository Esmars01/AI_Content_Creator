import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { EditProposal } from "@/lib/edits";
import { keys } from "@/lib/queries";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace, push: vi.fn() }) }));

const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, POST: (...args: unknown[]) => post(...args), GET: vi.fn() } };
});

const { ProposalCard } = await import("./edit-panel");

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

function withData(proposal: EditProposal, children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(keys.edit(proposal.id), proposal);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("ProposalCard", () => {
  beforeEach(() => {
    replace.mockReset();
    post.mockReset();
  });

  it("shows operations, changes, impact, coverage delta, alternatives and cost before anything changes", () => {
    render(withData(PROPOSAL, <ProposalCard proposalId="p1" videoId="vid" />));
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
    render(withData(PROPOSAL, <ProposalCard proposalId="p1" videoId="vid" />));
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
    render(withData(failed, <ProposalCard proposalId="p1" videoId="vid" />));
    expect(screen.getByLabelText("Issues").textContent).toContain("`camera` lock");
    expect(screen.queryByTestId("apply-edit")).toBeNull();
    expect(screen.getByRole("button", { name: "Reject" })).toBeTruthy();
  });
});

type Json = Record<string, unknown>;
