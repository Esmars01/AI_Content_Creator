import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const get = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, GET: (...args: unknown[]) => get(...args) } };
});

const { Player } = await import("./player");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

describe("Player", () => {
  it("offers the final render as a download and keeps the provenance caption (DL-01)", async () => {
    get.mockImplementation((path: string) => {
      if (path === "/v1/versions/{version_id}/renders")
        return ok([
          {
            id: "r1",
            version_id: "v1",
            preset_id: "tiktok_1080x1920_30",
            aspect: "9:16",
            is_proxy: false,
            status: "ready",
            provenance_mode: "mock_dev",
            artifact_id: "a1",
            watermark_payload_id: null,
            created_at: "2026-10-07T10:00:00Z",
          },
        ]);
      if (path === "/v1/renders/{render_id}/download")
        return ok({
          url: "https://s3.test/r1.mp4",
          expires_at: new Date(Date.now() + 600_000).toISOString(),
          exportable: false,
        });
      return ok(null);
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Player versionId="v1" state="ready" />
      </QueryClientProvider>,
    );
    const link = (await screen.findByRole("link", { name: "Download" })) as HTMLAnchorElement;
    expect(link.href).toBe("https://s3.test/r1.mp4");
    expect(link.hasAttribute("download")).toBe(true);
    expect(screen.getByText(/mock provenance \(not exportable\)/)).toBeTruthy();
  });
});
