/**
 * Same-origin gateway to the API service: the browser calls `/api/v1/...`; this handler forwards
 * method, body and the headers the API reads, and streams the answer back (SSE included). Session
 * cookies are therefore first-party to the web app, and no CORS policy is needed. It holds no
 * business logic and adds no credentials of its own.
 */
import type { NextRequest } from "next/server";

import { clientAddress } from "@/lib/client-address";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const API = (process.env.API_INTERNAL_URL || process.env.API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

const REQUEST_HEADERS = [
  "accept",
  "authorization",
  "content-type",
  "cookie",
  "idempotency-key",
  "last-event-id",
  "user-agent",
  "x-csrf-token",
  "x-request-id",
];
// hop-by-hop, or no longer true once fetch has decoded the body
const DROP_RESPONSE_HEADERS = new Set([
  "connection",
  "content-encoding",
  "content-length",
  "keep-alive",
  "set-cookie",
  "transfer-encoding",
  "upgrade",
]);

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }): Promise<Response> {
  const { path } = await context.params;
  const target = new URL(`${API}/${path.map(encodeURIComponent).join("/")}`);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const client = clientAddress(request.headers.get("x-forwarded-for"));
  if (client) headers.set("x-forwarded-for", client);
  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers,
    redirect: "manual",
    cache: "no-store",
    signal: request.signal,
  };
  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = request.body;
    init.duplex = "half";
  }
  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return Response.json(
      {
        type: "urn:ce:problem:api_unreachable",
        title: "The API service is unreachable",
        status: 502,
        code: "api_unreachable",
        detail: "The web app could not reach the API service.",
        instance: request.nextUrl.pathname,
      },
      { status: 502, headers: { "content-type": "application/problem+json" } },
    );
  }
  const out = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!DROP_RESPONSE_HEADERS.has(name.toLowerCase())) out.append(name, value);
  });
  for (const cookie of upstream.headers.getSetCookie()) out.append("set-cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export { forward as DELETE, forward as GET, forward as PATCH, forward as POST, forward as PUT };
