import { describe, expect, it } from "vitest";

import config, { SECURITY_HEADERS } from "../../next.config";

describe("security headers (§33)", () => {
  it("are sent on every route", async () => {
    const rules = config.headers ? await config.headers() : [];
    expect(rules).toEqual([{ source: "/:path*", headers: SECURITY_HEADERS }]);
    const byKey = Object.fromEntries(SECURITY_HEADERS.map((h) => [h.key, h.value]));
    expect(byKey["X-Content-Type-Options"]).toBe("nosniff");
    expect(byKey["X-Frame-Options"]).toBe("DENY");
    const csp = byKey["Content-Security-Policy"] ?? "";
    for (const directive of ["default-src 'self'", "frame-ancestors 'none'", "object-src 'none'", "base-uri 'self'"]) {
      expect(csp).toContain(directive);
    }
    // the browser uploads to the object store with presigned PUTs: https stores and the dev store
    const connect = /connect-src ([^;]*)/.exec(csp)?.[1] ?? "";
    expect(connect.split(" ")).toEqual(expect.arrayContaining(["'self'", "https:", "http://localhost:8333"]));
    expect(connect.split(" ")).not.toContain("http:"); // no arbitrary plain-HTTP origin
  });
});
