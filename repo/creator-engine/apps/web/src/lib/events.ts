"use client";
/**
 * Live updates over server-sent events (`GET /v1/events`, resumable with Last-Event-ID, §30).
 * One EventSource per tab (org scope); each event invalidates or patches the TanStack Query
 * caches it affects. `invalidationsFor` is pure so it can be tested without a browser.
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

const str = (value: unknown): string | null => (typeof value === "string" ? value : null);

/** Query keys an event makes stale. */
export function invalidationsFor(type: string, data: Data): Key[] {
  const version = str(data.version_id);
  switch (type) {
    case "job.updated": {
      const out: Key[] = [keys.jobs(), keys.job(String(data.job_id))];
      if (data.target_type === "video" && str(data.target_id)) out.push(keys.video(String(data.target_id)));
      if (data.target_type === "video_version" && str(data.target_id)) out.push(keys.version(String(data.target_id)));
      if (data.status === "succeeded" || data.status === "failed") out.push(keys.videosAll());
      return out;
    }
    case "version.updated":
      return version
        ? [keys.version(version), keys.previz(version), keys.storyboard(version), keys.videosAll(), keys.versionsAll()]
        : [];
    case "previz.ready":
      return version ? [keys.previz(version), keys.storyboard(version), keys.version(version)] : [];
    case "render.ready":
      return version ? [keys.renders(version)] : [];
    case "coverage.updated":
      return version ? [keys.coverage(version)] : [];
    case "node.updated":
      return str(data.job_id) ? [keys.job(String(data.job_id))] : [];
    case "memory.proposed":
    case "memory.conflict":
      return [["memory"]];
    case "stream.gap":
      return [[]]; // events were trimmed: refetch everything that is mounted
    default:
      return [];
  }
}

export function applyEvent(client: QueryClient, type: string, data: Data): void {
  for (const key of invalidationsFor(type, data)) {
    void client.invalidateQueries({ queryKey: key as unknown[] });
  }
}

export function useLiveEvents(enabled: boolean): void {
  const client = useQueryClient();
  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;
    const source = new EventSource("/api/v1/events?scope=org");
    const listeners = EVENT_TYPES.map((type) => {
      const listener = (event: MessageEvent<string>) => {
        try {
          applyEvent(client, type, JSON.parse(event.data) as Data);
        } catch {
          // a malformed event must not break the stream
        }
      };
      source.addEventListener(type, listener);
      return [type, listener] as const;
    });
    return () => {
      for (const [type, listener] of listeners) source.removeEventListener(type, listener);
      source.close();
    };
  }, [client, enabled]);
}
