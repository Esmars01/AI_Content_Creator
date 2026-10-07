import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { OfflineNotice } from "./offline-notice";

function setOnline(value: boolean) {
  Object.defineProperty(window.navigator, "onLine", { configurable: true, get: () => value });
  window.dispatchEvent(new Event(value ? "online" : "offline"));
}

describe("OfflineNotice", () => {
  afterEach(() => setOnline(true));

  it("says the browser is offline and goes away when it is back (BREAK-OFFLINE)", () => {
    // Regression: offline, Propose read "Sending…" with no reason until the connection came back.
    render(<OfflineNotice />);
    expect(screen.queryByTestId("offline-notice")).toBeNull();
    act(() => setOnline(false));
    expect(screen.getByTestId("offline-notice").textContent).toContain("You are offline");
    act(() => setOnline(true));
    expect(screen.queryByTestId("offline-notice")).toBeNull();
  });
});
