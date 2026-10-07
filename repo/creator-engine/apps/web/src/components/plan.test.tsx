import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, GET: vi.fn(() => new Promise(() => undefined)), POST: vi.fn() } };
});

const { ApprovePanel, ReplanPanel } = await import("./plan");

function wrap(children: ReactNode) {
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

describe("previz actions", () => {
  it("offers Regenerate plan only for planned versions (D2)", () => {
    // Regression: derived versions offered it and the API refused ("not planned from a request").
    const { unmount } = render(wrap(<ReplanPanel versionId="v2" videoId="vid" disabled={false} origin="edit" />));
    expect(screen.queryByRole("button", { name: "Regenerate plan" })).toBeNull();
    expect(screen.getByTestId("derived-version-note").textContent).toContain(
      "Derived versions are changed with edits in the Studio",
    );
    expect(screen.getByRole("link", { name: "Edit in the studio" }).getAttribute("href")).toBe(
      "/videos/vid?version=v2#edit",
    );
    unmount();
    for (const origin of ["plan", "replan"]) {
      const view = render(wrap(<ReplanPanel versionId="v1" videoId="vid" disabled={false} origin={origin} />));
      expect((screen.getByRole("button", { name: "Regenerate plan" }) as HTMLButtonElement).disabled).toBe(false);
      view.unmount();
    }
  });

  it("holds approval while the plan needs a proposed world version approved (D9)", () => {
    // Regression: Approve stayed enabled and the API refused ("approve the proposed world version first").
    render(
      wrap(
        <ApprovePanel
          versionId="v1"
          videoId="vid"
          blocking={[]}
          disabled={false}
          state="previz_ready"
          needsWorldApproval
          worldHref="/worlds/w1"
        />,
      ),
    );
    expect((screen.getByRole("button", { name: "Approve and generate" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("needs-world-approval").textContent).toContain("proposed world version");
    expect(screen.getByRole("link", { name: "World Studio" }).getAttribute("href")).toBe("/worlds/w1");
  });
});
