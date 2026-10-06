import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, CSRF_HEADER, idempotencyKey, makeClient, readCookie, unwrap } from "./api";

afterEach(() => {
  document.cookie = "ce_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
});

describe("api client", () => {
  it("reads cookies", () => {
    expect(readCookie("ce_csrf", "a=1; ce_csrf=tok%3D1; b=2")).toBe("tok=1");
    expect(readCookie("missing", "a=1")).toBeNull();
  });

  it("sends the CSRF token on changes only", async () => {
    document.cookie = "ce_csrf=secret-token";
    const seen: Request[] = [];
    const fetchMock = vi.fn(async (input: Request) => {
      seen.push(input);
      return new Response(JSON.stringify({ items: [], next_cursor: null }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    });
    const client = makeClient({ baseUrl: "http://web.test/api", fetch: fetchMock as unknown as typeof fetch });
    await client.GET("/v1/projects");
    await client.POST("/v1/projects", { body: { name: "P", description: "" } });
    expect(seen[0]?.headers.get(CSRF_HEADER)).toBeNull();
    expect(seen[1]?.headers.get(CSRF_HEADER)).toBe("secret-token");
    expect(seen[1]?.url).toBe("http://web.test/api/v1/projects");
  });

  it("turns problem documents into ApiError", async () => {
    const problem = {
      type: "urn:ce:problem:approval_blocked",
      title: "Approval requirements are not met",
      status: 409,
      code: "approval_blocked",
      detail: "the version cannot be approved yet",
      instance: "/v1/versions/x:approve",
      issues: [{ code: "blocking_finding", message: "Unsupported claim", path: null, severity: "error" }],
    };
    const call = Promise.resolve({ error: problem, response: new Response(null, { status: 409 }) });
    const error = await unwrap(call).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("approval_blocked");
    expect((error as ApiError).message).toBe("the version cannot be approved yet");
    expect((error as ApiError).issues[0]?.code).toBe("blocking_finding");
  });

  it("makes unique idempotency keys", () => {
    expect(idempotencyKey()["Idempotency-Key"]).not.toBe(idempotencyKey()["Idempotency-Key"]);
  });
});
