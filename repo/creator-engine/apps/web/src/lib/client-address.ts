/**
 * The client address for the API's per-address login limit: only the rightmost `X-Forwarded-For`
 * entry, the one written by the nearest hop (Next.js itself when the app is exposed directly, the
 * operator's reverse proxy in production). Entries to its left come from the client and are
 * ignored, so a browser cannot choose its own rate-limit bucket by sending the header; behind a
 * proxy that appends `$remote_addr` the value is the real peer (docs/SECURITY.md).
 */
export function clientAddress(forwardedFor: string | null): string | null {
  const hops = (forwardedFor ?? "")
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);
  const last = hops.at(-1);
  return last && /^[0-9A-Fa-f:.]{2,45}$/.test(last) ? last : null;
}
