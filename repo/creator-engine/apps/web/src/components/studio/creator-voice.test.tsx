import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
const get = vi.fn();
const patch = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      GET: (...args: unknown[]) => get(...args),
      PATCH: (...args: unknown[]) => patch(...args),
      POST: vi.fn(),
    },
  };
});

const { VoiceVersionBench } = await import("./creator-panels");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

describe("voice lexicon (LEXICON-REMOVE)", () => {
  it("removes a term from a draft voice version", async () => {
    // Regression: terms could be added but never removed.
    const lexicon = [
      { term: "Robin", respelling: "ROB-in" },
      { term: "Kyiv", respelling: "KEE-iv" },
    ];
    get.mockImplementation(() => ok({ id: "vv1", number: 2, status: "draft", lexicon, wpm: {} }));
    patch.mockImplementation(() => ok({ id: "vv1" }));
    render(
      <QueryClientProvider client={new QueryClient()}>
        <VoiceVersionBench versionId="vv1" />
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole("button", { name: "Remove Robin" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(patch.mock.calls[0]?.[1]).toEqual({
      params: { path: { voice_version_id: "vv1" } },
      body: { lexicon: [{ term: "Kyiv", respelling: "KEE-iv" }] },
    });
  });
});
