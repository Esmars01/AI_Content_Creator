// @vitest-environment node
import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";

import { clientAddress } from "@/lib/client-address";

import { GET, POST } from "./route";

const ctx = (path: string[]) => ({ params: Promise.resolve({ path }) });

afterEach(() => vi.unstubAllGlobals());

describe("API gateway route", () => {
  it("forwards the headers the API reads and passes cookies and status back", async () => {
    const seen: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: URL, init: RequestInit) => {
        seen.push({ url: String(url), init });
        const headers = new Headers({ "content-type": "application/json", "content-encoding": "gzip" });
        headers.append("set-cookie", "ce_session=abc; HttpOnly; Path=/");
        headers.append("set-cookie", "ce_csrf=tok; Path=/");
        return new Response('{"ok":true}', { status: 201, headers });
      }),
    );
    const request = new NextRequest("http://web.test/api/v1/projects?limit=5", {
      method: "POST",
      headers: {
        cookie: "ce_session=old",
        "x-csrf-token": "tok",
        "idempotency-key": "k1",
        "content-type": "application/json",
        "x-secret-internal": "must-not-pass",
      },
      body: JSON.stringify({ name: "P" }),
    });
    const response = await POST(request, ctx(["v1", "projects"]));
    expect(seen[0]?.url).toBe("http://localhost:8000/v1/projects?limit=5");
    const sent = new Headers(seen[0]?.init.headers);
    expect(sent.get("x-csrf-token")).toBe("tok");
    expect(sent.get("idempotency-key")).toBe("k1");
    expect(sent.get("cookie")).toBe("ce_session=old");
    expect(sent.get("x-secret-internal")).toBeNull();
    expect(response.status).toBe(201);
    expect(response.headers.get("content-encoding")).toBeNull(); // fetch already decoded the body
    expect(response.headers.getSetCookie()).toEqual(["ce_session=abc; HttpOnly; Path=/", "ce_csrf=tok; Path=/"]);
    expect(await response.json()).toEqual({ ok: true });
  });

  it("answers 502 with a problem document when the API is down", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );
    const response = await GET(new NextRequest("http://web.test/api/v1/me"), ctx(["v1", "me"]));
    expect(response.status).toBe(502);
    expect((await response.json()).code).toBe("api_unreachable");
  });

  it("forwards only the nearest hop's client address", async () => {
    expect(clientAddress("6.6.6.6, 203.0.113.7")).toBe("203.0.113.7"); // a spoofed entry on the left is ignored
    expect(clientAddress("203.0.113.7")).toBe("203.0.113.7");
    expect(clientAddress("2001:db8::1")).toBe("2001:db8::1");
    expect(clientAddress(null)).toBeNull();
    expect(clientAddress("not-an-address")).toBeNull();
    const seen: RequestInit[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: URL, init: RequestInit) => {
        seen.push(init);
        return new Response("{}", { status: 200 });
      }),
    );
    await GET(
      new NextRequest("http://web.test/api/v1/me", { headers: { "x-forwarded-for": "6.6.6.6, 198.51.100.9" } }),
      ctx(["v1", "me"]),
    );
    expect(new Headers(seen[0]?.headers).get("x-forwarded-for")).toBe("198.51.100.9");
  });
});
