import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { deniedReason, roleHas } from "@/lib/roles";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { EditPanel } = await import("./edit-panel");
const { ApprovePanel } = await import("./plan");
const { RoleNote } = await import("./role-note");
const { VersionsPanel } = await import("./studio-panels");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

function asRole(role: string, children: ReactNode) {
  get.mockImplementation((path: string) => {
    if (path === "/v1/me") return ok({ role, user: { id: "u1" }, org: { id: "o1" }, auth: "session", memberships: [] });
    return ok([]);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

const VERSIONS = [
  { id: "v1", number: 1, origin: "plan", state: "ready", branch: "main", parent_version_id: null },
  { id: "v2", number: 2, origin: "edit", state: "ready", branch: "main", parent_version_id: "v1" },
] as never[];

function Studio() {
  return (
    <>
      <RoleNote />
      <EditPanel versionId="v2" sceneKeys={[]} />
      <VersionsPanel videoId="vid" versions={VERSIONS} currentId="v2" />
      <ApprovePanel versionId="v2" videoId="vid" blocking={[]} disabled={false} state="previz_ready" />
    </>
  );
}

const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("role gating (ROLES)", () => {
  it("mirrors the API's RBAC table", () => {
    expect(roleHas("viewer", "write_content")).toBe(false);
    expect(roleHas("developer", "write_content")).toBe(false);
    expect(roleHas("editor", "write_content")).toBe(true);
    expect(roleHas("developer", "manage_api_keys")).toBe(true);
    expect(roleHas("editor", "manage_org")).toBe(false);
    expect(deniedReason("viewer", "write_content")).toBe("Your role (viewer) can view but not change content.");
  });

  it("disables (not hides) the write controls for a viewer and says why", async () => {
    // Regression: the app never read the role, so viewers saw every control enabled and got a 403.
    render(asRole("viewer", <Studio />));
    expect((await screen.findByTestId("role-note")).textContent).toBe(
      "Your role (viewer) can view but not change content.",
    );
    fireEvent.change(screen.getByLabelText("Instruction"), { target: { value: "more skeptical" } });
    fireEvent.change(screen.getByLabelText("Branch name"), { target: { value: "alt" } });
    const reason = "Your role (viewer) can view but not change content.";
    for (const name of ["Propose", "Restore", "Branch", "Approve and generate"]) {
      expect(button(name).disabled).toBe(true);
      expect(button(name).title).toBe(reason);
    }
    fireEvent.submit(button("Propose").closest("form") as HTMLFormElement);
    expect(post).not.toHaveBeenCalled();
  });

  it("keeps the controls enabled for an editor", async () => {
    render(asRole("editor", <Studio />));
    fireEvent.change(screen.getByLabelText("Instruction"), { target: { value: "more skeptical" } });
    fireEvent.change(screen.getByLabelText("Branch name"), { target: { value: "alt" } });
    await waitFor(() => expect(get).toHaveBeenCalledWith("/v1/me"));
    expect(screen.queryByTestId("role-note")).toBeNull();
    for (const name of ["Propose", "Restore", "Branch", "Approve and generate"]) {
      expect(button(name).disabled).toBe(false);
    }
  });
});
