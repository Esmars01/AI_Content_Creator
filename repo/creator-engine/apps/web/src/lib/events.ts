"use client";
/**
 * Live updates over server-sent events (`GET /v1/events`, resumable with Last-Event-ID, §30).
 * One EventSource per tab (org scope); each event invalidates the TanStack Query caches it
 * affects. Events are a fast path, never the only one: views that wait for an outcome also poll
 * the durable API state, and after a reconnect (or when a hidden tab becomes visible) every
 * mounted query is refetched, so a missed event cannot leave the UI stale. `invalidationsFor` is
 * pure so it can be tested without a browser.
 */
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { keys } from "./queries";

export const EVENT_TYPES = [
  "job.updated",
  "node.updated",
  "version.updated",
  "previz.ready",
  "render.ready",
  "coverage.updated",
  "memory.proposed",
  "memory.conflict",
  "stream.gap",
] as const;
export type EventType = (typeof EVENT_TYPES)[number];

export interface JobEvent {
  job_id: string;
  kind: string;
  status: string;
  progress: number;
  target_type: string;
  target_id: string;
  error?: unknown;
}

type Data = Record<string, unknown>;
type Key = readonly unknown[];
/** A key to invalidate; `exact` limits it to that query instead of everything under the prefix. */
export interface Invalidation {
  queryKey: Key;
  exact?: boolean;
}

const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);

const str = (value: unknown): string | null => (typeof value === "string" ? value : null);

/** Query keys an event makes stale. */
export function invalidationsFor(type: string, data: Data): Invalidation[] {
  const version = str(data.version_id);
  const all = (...ks: Key[]) => ks.map((queryKey) => ({ queryKey }));
  switch (type) {
    case "job.updated": {
      const out: Invalidation[] = all(keys.jobs(), keys.job(String(data.job_id)));
      const target = str(data.target_id);
      const terminal = TERMINAL.has(String(data.status));
      if (data.target_type === "video" && target) out.push({ queryKey: keys.video(target) });
      if (data.target_type === "video_version" && target) {
        // Every finished node of a build sends a job update. While running, only the version row
        // is refreshed (exact); at the end, everything under the version (renders, coverage, QC…).
        out.push(terminal ? { queryKey: keys.version(target) } : { queryKey: keys.version(target), exact: true });
      }
      if (terminal) out.push({ queryKey: keys.videosAll() });
      return out;
    }
    case "version.updated":
      return version ? all(keys.version(version), keys.videosAll(), keys.versionsAll()) : [];
    case "previz.ready":
      return version ? all(keys.previz(version), keys.storyboard(version), keys.version(version)) : [];
    case "render.ready":
      return version ? all(keys.renders(version)) : [];
    case "coverage.updated":
      return version ? all(keys.coverage(version)) : [];
    case "node.updated":
      return str(data.job_id) ? all(keys.job(String(data.job_id))) : [];
    case "memory.proposed":
    case "memory.conflict":
      return all(["memory"]);
    case "stream.gap":
      return all([]); // events were trimmed: refetch everything that is mounted
    default:
      return [];
  }
}

export function applyEvent(client: QueryClient, type: string, data: Data): void {
  for (const { queryKey, exact } of invalidationsFor(type, data)) {
    void client.invalidateQueries({ queryKey: queryKey as unknown[], exact });
  }
}

/** Reconnect delay after the stream closed for good (non-200, gateway down): 1 s, 2 s, 4 s … 30 s. */
export function reconnectDelay(attempt: number): number {
  return Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
}

export function useLiveEvents(enabled: boolean): void {
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let opened = false;
    let stopped = false;
    let hiddenAt: number | null = null;
    // durable state wins: refetch whatever is on screen
    const resync = () => void client.invalidateQueries({ refetchType: "active" });

    const connect = () => {
      const current = new EventSource("/api/v1/events?scope=org");
      source = current;
      current.onopen = () => {
        if (opened) resync(); // a reconnect: events may have been missed while disconnected
        opened = true;
        attempt = 0;
      };
      current.onerror = () => {
        // The browser retries by itself while readyState is CONNECTING; CLOSED (a non-200 or
        // non-event-stream answer, e.g. a 502 while the API restarts) is final, so reconnect here.
        if (current.readyState !== EventSource.CLOSED || stopped) return;
        current.close();
        timer = setTimeout(connect, reconnectDelay(attempt++));
      };
      for (const type of EVENT_TYPES) {
        current.addEventListener(type, (event: MessageEvent<string>) => {
          try {
            applyEvent(client, type, JSON.parse(event.data) as Data);
          } catch {
            // a malformed event must not break the stream
          }
        });
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (hiddenAt !== null && Date.now() - hiddenAt > 30_000) {
        hiddenAt = null;
        resync(); // a sleeping tab may have missed events (or its stream)
      }
    };
    connect();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      source?.close();
    };
  }, [client, enabled]);
}
