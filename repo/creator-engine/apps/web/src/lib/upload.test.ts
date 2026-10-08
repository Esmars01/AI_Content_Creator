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

const { mimeOf, uploadAsset, UploadRejectedError } = await import("./upload");

const ok = (data: unknown) => Promise.resolve({ data, response: new Response(null, { status: 200 }) });
const plan = {
  asset_id: "a1",
  upload: { part_size: 4, parts: [1, 2].map((n) => ({ part_number: n, url: `https://s3.test/${n}`, headers: {} })) },
};

describe("uploadAsset", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    get.mockReset();
    post.mockReset();
  });

  it("uploads every part, completes, and waits until the asset is ready", async () => {
    post.mockImplementation((path: string) => ok(path === "/v1/assets:initiate-upload" ? plan : { job_id: "j1" }));
    const statuses = ["uploading", "uploading", "ready"];
    get.mockImplementation(() => ok({ id: "a1", status: statuses.shift(), probe: {} }));
    const puts: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        puts.push(url);
        return Promise.resolve(new Response(null, { status: 200 }));
      }),
    );
    const stages: string[] = [];
    const asset = await uploadAsset(new File(["12345678"], "face.png", { type: "image/png" }), {
      kind: "image",
      onStage: (s) => stages.push(s),
      sleep: () => Promise.resolve(),
    });
    expect(asset.status).toBe("ready");
    expect(puts).toEqual(["https://s3.test/1", "https://s3.test/2"]);
    expect(post.mock.calls.map((c) => c[0])).toEqual(["/v1/assets:initiate-upload", "/v1/assets/{asset_id}:complete"]);
    expect(get).toHaveBeenCalledTimes(3);
    expect(stages).toEqual(["uploading", "validating", "ready"]);
  });

  it("reports why the validation job rejected the file", async () => {
    post.mockImplementation((path: string) => ok(path === "/v1/assets:initiate-upload" ? plan : { job_id: "j1" }));
    get.mockImplementation(() =>
      ok({ id: "a1", status: "rejected", probe: { rejected: [{ code: "upload_sniff", message: "not image/png" }] } }),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 200 }))),
    );
    const upload = uploadAsset(new File(["x"], "face.png", { type: "image/png" }), {
      kind: "image",
      sleep: () => Promise.resolve(),
    });
    await expect(upload).rejects.toBeInstanceOf(UploadRejectedError);
    await expect(upload).rejects.toThrow("not image/png");
  });

  it("fails a part that the storage refuses", async () => {
    post.mockImplementation(() => ok(plan));
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(new Response(null, { status: 403 }))),
    );
    await expect(uploadAsset(new File(["x"], "a.wav"), { kind: "audio" })).rejects.toThrow("upload failed (403)");
  });

  it("derives a MIME type from the extension when the browser gives none", () => {
    expect(mimeOf(new File(["x"], "voice.WAV"))).toBe("audio/wav");
    expect(mimeOf(new File(["x"], "notes.unknown"), "text/plain")).toBe("text/plain");
  });
});
