import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { Alert, LoadError } from "./misc";

describe("status messages", () => {
  it("a failed load is an alert with the reason and a retry, never an empty state", () => {
    const retry = vi.fn();
    render(<LoadError what="creators" error={new Error("502 Bad Gateway")} onRetry={retry} />);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Could not load creators: 502 Bad Gateway");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("only danger alerts interrupt; the others are announced politely", () => {
    render(<Alert tone="info">Planning is under way…</Alert>);
    expect(screen.getByRole("status").textContent).toBe("Planning is under way…");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
