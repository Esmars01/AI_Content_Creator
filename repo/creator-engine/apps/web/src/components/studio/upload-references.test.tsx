import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
const post = vi.fn();
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, GET: (...args: unknown[]) => get(...args), POST: (...args: unknown[]) => post(...args) },
  };
});

const { ATTESTATION_TEXT, UploadReference, VoiceImport } = await import("./upload-references");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });

function mockUpload() {
  post.mockImplementation((path: string) =>
    ok(
      path === "/v1/assets:initiate-upload"
        ? {
            asset_id: "a1",
            upload: { part_size: 1024, parts: [{ part_number: 1, url: "https://s3.test/a1", headers: {} }] },
          }
        : { job_id: "j1" },
    ),
  );
  get.mockImplementation(() => ok({ id: "a1", status: "ready", probe: {} }));
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve(new Response(null, { status: 200 }))),
  );
}

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      {ui}
    </QueryClientProvider>,
  );

describe("UploadReference", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    get.mockReset();
    post.mockReset();
  });

  it("needs a file and the attestation, then hands over the validated asset", async () => {
    mockUpload();
    const onUploaded = vi.fn(() => Promise.resolve());
    wrap(
      <UploadReference label="Face" accept="image/png" kind="reference" attestation="face" onUploaded={onUploaded} />,
    );
    const button = screen.getByRole("button", { name: "Upload" });
    expect((button as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Face"), {
      target: { files: [new File(["png"], "face.png", { type: "image/png" })] },
    });
    expect((button as HTMLButtonElement).disabled).toBe(true); // no attestation yet
    fireEvent.click(screen.getByLabelText(ATTESTATION_TEXT.face));
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() => expect(onUploaded).toHaveBeenCalledWith("a1"));
    expect(post).toHaveBeenCalledWith(
      "/v1/assets:initiate-upload",
      expect.objectContaining({ body: expect.objectContaining({ kind: "reference", mime: "image/png" }) }),
    );
  });

  it("says that real people need the consent flow", () => {
    expect(ATTESTATION_TEXT.face).toContain("not a photo of a real, identifiable person");
    expect(ATTESTATION_TEXT.voice).toContain("not a recording of a real person's voice");
  });

  it("imports a voice only with a name, language and transcript", async () => {
    mockUpload();
    const onImport = vi.fn(() => Promise.resolve());
    wrap(<VoiceImport onImport={onImport} />);
    const button = screen.getByRole("button", { name: "Upload and create the voice" });
    fireEvent.change(screen.getByLabelText(/Reference recording/), {
      target: { files: [new File(["wav"], "v.wav", { type: "audio/wav" })] },
    });
    fireEvent.click(screen.getByLabelText(ATTESTATION_TEXT.voice));
    expect((button as HTMLButtonElement).disabled).toBe(true); // no name or transcript
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Narrator" } });
    fireEvent.change(screen.getByLabelText(/Transcript/), { target: { value: "Hello there." } });
    expect((button as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(button);
    await waitFor(() =>
      expect(onImport).toHaveBeenCalledWith({
        assetId: "a1",
        name: "Narrator",
        language: "en-US",
        transcript: "Hello there.",
      }),
    );
  });
});
