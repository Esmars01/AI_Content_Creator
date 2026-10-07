import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }), usePathname: () => "/videos/vid" }));

const { usePinShownVersion } = await import("./pin-version");
const { useStudio } = await import("./store");

describe("usePinShownVersion", () => {
  beforeEach(() => {
    replace.mockReset();
    useStudio.setState({ activeProposals: {} });
  });

  it("pins the Studio URL to the version a proposal is open on (BREAK-TWO-TABS)", () => {
    // Regression: another tab applied an edit, this tab followed the new current version, and the
    // proposal it had open on the old one disappeared without a word.
    renderHook(() => usePinShownVersion("v3", false));
    expect(replace).not.toHaveBeenCalled();
    act(() => useStudio.getState().showProposal("v3", "p1"));
    expect(replace).toHaveBeenCalledWith("/videos/vid?version=v3", { scroll: false });
  });

  it("leaves an explicit version link alone", () => {
    useStudio.getState().showProposal("v3", "p1");
    renderHook(() => usePinShownVersion("v3", true));
    expect(replace).not.toHaveBeenCalled();
  });
});
