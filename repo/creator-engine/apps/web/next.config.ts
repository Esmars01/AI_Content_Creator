import type { NextConfig } from "next";

// Security headers (§33 "security headers on web"). Media and images come from the object store
// through presigned URLs (another origin), so img/media sources allow https: and http: (the dev
// store is http://localhost:8333). The browser also uploads straight to the object store
// (presigned PUTs), so connections allow https: object stores plus the plain-HTTP origins in
// WEB_CSP_STORAGE_ORIGINS (read at build time; default: the local dev store). Scripts, styles and
// frames stay same-origin. Next.js inlines its bootstrap scripts, hence 'unsafe-inline' for
// scripts; development also needs 'unsafe-eval' (React Refresh).
const dev = process.env.NODE_ENV !== "production";
const storageOrigins = (process.env.WEB_CSP_STORAGE_ORIGINS ?? "http://localhost:8333 http://127.0.0.1:8333").trim();
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https: http:",
  "media-src 'self' blob: https: http:",
  "font-src 'self' data:",
  `connect-src 'self' https:${storageOrigins ? ` ${storageOrigins}` : ""}${dev ? " ws:" : ""}`,
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

export const SECURITY_HEADERS: { key: string; value: string }[] = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
  { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=()" },
  { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  // HSTS only where the deployment serves HTTPS (WEB_HSTS=true); never on plain-HTTP development
  ...(process.env.WEB_HSTS === "true"
    ? [{ key: "Strict-Transport-Security", value: "max-age=31536000; includeSubDomains" }]
    : []),
];

// The browser talks only to this app; `/api/*` is forwarded to the API service by a route handler
// (src/app/api/[...path]/route.ts), so session cookies stay first-party and no CORS is needed.
const config: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Server-sent events must not be buffered by response compression; a fronting proxy compresses.
  compress: false,
  poweredByHeader: false,
  transpilePackages: ["@ce/api-client"],
  outputFileTracingRoot: new URL("../../", import.meta.url).pathname,
  async headers() {
    return [{ source: "/:path*", headers: SECURITY_HEADERS }];
  },
};

export default config;
