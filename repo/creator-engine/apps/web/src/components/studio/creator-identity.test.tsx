import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push }) }));
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

const { DnaEditor, ageCheckText } = await import("./creator-panels");
const { NewCreatorForm, NewWorldForm } = await import("./new-identity");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

/** The path and init of a mock's last call. */
type SentDna = { identity: { display_name: string }; [field: string]: unknown };
function lastCall(mock: ReturnType<typeof vi.fn>): [string, { body: { name?: string; dna: SentDna } }] {
  const call = mock.mock.calls.at(-1);
  if (!call) throw new Error("not called");
  return call as never;
}

const DRAFT = {
  id: "cv2",
  number: 2,
  status: "draft",
  dna: { vocab_version: "2026.10.1", identity: { display_name: "Alex", canon: [] } },
  appearance_version_id: "av1",
  voice_version_id: "vv1",
  default_world_ids: [],
  default_wardrobe_version_ids: ["wv1"],
};

function routes(path: string, init?: { params?: { path?: Record<string, string> } }) {
  if (path === "/v1/me") return ok({ role: "editor", user: { id: "u1" }, org: { id: "o1" }, memberships: [] });
  if (path === "/v1/creator-versions/{creator_version_id}") {
    const id = init?.params?.path?.creator_version_id;
    return ok(
      id === "src1"
        ? {
            ...DRAFT,
            id: "src1",
            dna: {
              vocab_version: "2026.10.1",
              identity: { display_name: "Alex", bio: "x", canon: [] },
              personality: { humor: "dry" },
            },
          }
        : DRAFT,
    );
  }
  if (path === "/v1/creators/{creator_id}/appearances")
    return ok([
      {
        id: "a1",
        name: "Default look",
        versions: [
          { id: "av1", number: 1, status: "approved" },
          { id: "av2", number: 2, status: "approved" },
        ],
      },
    ]);
  if (path === "/v1/creators/{creator_id}/wardrobes")
    return ok([{ id: "w1", name: "grey hoodie", versions: [{ id: "wv1", number: 1, status: "approved" }] }]);
  if (path === "/v1/voices")
    return ok([
      {
        id: "voice1",
        name: "Alex voice",
        current_version_id: "vv2",
        creator_id: "c1",
        kind: "designed",
        created_at: "",
      },
    ]);
  if (path === "/v1/worlds")
    return ok({
      items: [
        {
          id: "world1",
          name: "Alex's home office",
          current_version_id: "wver1",
          kind: "home_office",
          status: "approved",
        },
      ],
      next_cursor: null,
    });
  if (path === "/v1/world-versions/{world_version_id}")
    return ok({
      id: "wver1",
      dna: {
        vocab_version: "2026.10.1",
        name: "Alex's home office",
        kind: "home_office",
        fingerprints_artifact_id: "art1",
        camera_positions: [{ key: "cam" }],
      },
    });
  if (path === "/v1/vocabulary")
    return ok({ version: "2026.10.1", categories: {}, lock_groups: [], regenerate_components: [] });
  if (path === "/v1/creators") return ok({ items: [{ id: "src", name: "Alex" }], next_cursor: null });
  if (path === "/v1/creators/{creator_id}")
    return ok({ id: "src", name: "Alex", current_version_id: "src1", versions: [] });
  return ok([]);
}

function wrap(children: ReactNode) {
  get.mockImplementation(routes);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("creator identity links (audit CR-VOICE)", () => {
  it("lets a draft point at another approved voice and look, and saves the links", async () => {
    // Before: the draft editor sent `dna` only — nothing could make a newer voice or look the creator's.
    patch.mockReturnValue(ok(DRAFT));
    const creator = {
      id: "c1",
      name: "Alex",
      current_version_id: "cv1",
      versions: [
        { id: "cv1", number: 1, status: "approved" },
        { id: "cv2", number: 2, status: "draft" },
      ],
    };
    render(wrap(<DnaEditor creator={creator as never} />));
    const voice = await screen.findByLabelText("Voice");
    await waitFor(() => expect(screen.getByRole("option", { name: "Alex voice (its current version)" })).toBeTruthy());
    expect(screen.getByRole("option", { name: "The linked version (not its voice's current version)" })).toBeTruthy();
    fireEvent.change(voice, { target: { value: "vv2" } });
    fireEvent.change(screen.getByLabelText("Appearance"), { target: { value: "av2" } });
    fireEvent.click(await screen.findByRole("checkbox", { name: "Alex's home office" }));
    fireEvent.click(screen.getByRole("button", { name: "Save links" }));
    await waitFor(() => expect(patch).toHaveBeenCalled());
    expect(lastCall(patch)[1].body).toEqual({
      appearance_version_id: "av2",
      voice_version_id: "vv2",
      defaults: { world_ids: ["world1"], wardrobe_version_ids: ["wv1"] },
    });
  });
});

describe("new creator and new world (audit CR-CREATE)", () => {
  it("creates a draft creator with the name and bio, from a blank DNA", async () => {
    post.mockReturnValue(ok({ id: "new1" }));
    render(wrap(<NewCreatorForm />));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Maya" } });
    fireEvent.change(screen.getByLabelText("Bio"), { target: { value: "Talks about sleep science." } });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Create creator" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create creator" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/creators/new1"));
    const [path, init] = lastCall(post);
    expect(path).toBe("/v1/creators");
    expect(init.body).toEqual({
      name: "Maya",
      kind: "synthetic",
      dna: {
        vocab_version: "2026.10.1",
        identity: { canon: [], display_name: "Maya", bio: "Talks about sleep science." },
      },
    });
  });

  it("starts a creator from another creator's DNA under the new name", async () => {
    post.mockReturnValue(ok({ id: "new2" }));
    render(wrap(<NewCreatorForm />));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Sam" } });
    await screen.findByRole("option", { name: "Alex's DNA" });
    fireEvent.change(screen.getByLabelText("Start from"), { target: { value: "src" } });
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Create creator" }) as HTMLButtonElement).disabled).toBe(false),
    );
    fireEvent.click(screen.getByRole("button", { name: "Create creator" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/creators/new2"));
    const dna = lastCall(post)[1].body.dna;
    expect(dna.identity.display_name).toBe("Sam");
    expect(dna.personality).toEqual({ humor: "dry" }); // the source's DNA, not a blank one
  });

  it("creates a world as a renamed copy of an existing world's DNA, without its plate fingerprints", async () => {
    post.mockReturnValue(ok({ id: "neww" }));
    render(wrap(<NewWorldForm />));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Studio loft" } });
    await screen.findByRole("option", { name: "Alex's home office" });
    fireEvent.click(screen.getByRole("button", { name: "Create world" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/worlds/neww"));
    const [path, init] = lastCall(post);
    expect(path).toBe("/v1/worlds");
    expect(init.body.name).toBe("Studio loft");
    expect(init.body.dna).toEqual({
      vocab_version: "2026.10.1",
      name: "Studio loft",
      kind: "home_office",
      camera_positions: [{ key: "cam" }],
    });
  });
});

describe("the appearance age check text", () => {
  it("never shows 'undefined' for what was not measured", () => {
    // Before: "VLM apparent age null (threshold 25) via undefined; DNA age undefined."
    expect(ageCheckText({ vlm_estimate: null, threshold: 25 })).toBe(
      "VLM apparent age: not measured yet (threshold 25).",
    );
    expect(
      ageCheckText({
        vlm_estimate: 31,
        threshold: 25,
        vlm_adapter: "mock_vlm",
        vlm_mock: true,
        dna_age_appearance: 30,
      }),
    ).toBe("VLM apparent age 31 (threshold 25) via mock_vlm — mock answer, not evidence; DNA age 30.");
  });
});
