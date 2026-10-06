/**
 * The typed API client (openapi-fetch over the generated `@ce/api-client` types).
 *
 * The browser talks to this app's `/api/*` route, which forwards to the API service; session
 * cookies are first-party. Cookie-authenticated changes carry `X-CSRF-Token` from the readable
 * `ce_csrf` cookie (docs/API.md). Errors are RFC 9457 problems, raised as `ApiError`.
 */
import type { Api, ApiPaths, Models } from "@ce/api-client";
import createClient, { type Middleware } from "openapi-fetch";

export type Schemas = Api["schemas"];
export type Domain = Models["schemas"];
export type PlanReport = Domain["PlanReport"];
export type VideoSpec = Domain["VideoSpec"];
export type Problem = Schemas["Problem"];

export const CSRF_COOKIE = "ce_csrf";
export const CSRF_HEADER = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function readCookie(name: string, source?: string): string | null {
  const jar = source ?? (typeof document === "undefined" ? "" : document.cookie);
  for (const part of jar.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return decodeURIComponent(rest.join("="));
  }
  return null;
}

export const csrfMiddleware: Middleware = {
  onRequest({ request }) {
    if (!SAFE_METHODS.has(request.method.toUpperCase())) {
      const token = readCookie(CSRF_COOKIE);
      if (token) request.headers.set(CSRF_HEADER, token);
    }
    return request;
  },
};

function baseUrl(): string {
  if (typeof window !== "undefined") return `${window.location.origin}/api`;
  return `${process.env.WEB_ORIGIN ?? "http://localhost:3000"}/api`;
}

export function makeClient(options: { baseUrl?: string; fetch?: typeof fetch } = {}) {
  const client = createClient<ApiPaths>({
    baseUrl: options.baseUrl ?? baseUrl(),
    credentials: "same-origin",
    ...(options.fetch ? { fetch: options.fetch } : {}),
  });
  client.use(csrfMiddleware);
  return client;
}

export const api = makeClient();

export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;

  constructor(status: number, problem: Problem | null, fallback: string) {
    super(problem?.detail || problem?.title || fallback);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  get code(): string {
    return this.problem?.code ?? `http_${this.status}`;
  }

  get issues(): Schemas["ProblemIssue"][] {
    return this.problem?.issues ?? [];
  }
}

function asProblem(error: unknown): Problem | null {
  if (error && typeof error === "object" && "status" in error && "title" in error) return error as Problem;
  return null;
}

/** The response data, or an `ApiError` carrying the problem document. */
export async function unwrap<T>(call: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await call;
  if (error !== undefined || !response.ok) {
    throw new ApiError(response.status, asProblem(error), `${response.status} ${response.statusText}`);
  }
  return data as T;
}

/** A fresh `Idempotency-Key` header for POSTs that start work (§30). */
export function idempotencyKey(): { "Idempotency-Key": string } {
  return { "Idempotency-Key": crypto.randomUUID() };
}
