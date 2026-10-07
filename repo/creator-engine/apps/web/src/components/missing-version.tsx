"use client";
/**
 * A deep link to a version the API does not have (404, or an id that is not a UUID), and the
 * "is it there yet?" wait for a version this tab just asked for (an applied edit creates its version
 * in a job, so the row appears a little later).
 */
import Link from "next/link";
import type { RefObject } from "react";

import { Empty } from "@/components/ui/misc";
import { isMissing, MISSING_AFTER, useJob } from "@/lib/queries";
import { usePendingVersions } from "@/lib/store";

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "partial"]);

export function MissingVersion({ videoId }: { videoId: string }) {
  return (
    <Empty>
      <span data-testid="version-missing">This version does not exist.</span>{" "}
      <Link className="text-blue-800 underline" href={`/videos/${videoId}`}>
        Open the video&apos;s current version
      </Link>
    </Empty>
  );
}

/**
 * Whether a version query that has no data is a missing version, or one still being created:
 * - `missing`: a 422, or a 404 seen `MISSING_AFTER` times with no job of this tab still creating it
 *   (and no `creatingJobId`, e.g. the plan job of a link with `?job=`, still running);
 * - `creating`: a 404 while such a job runs.
 * The caller's version query polls while `keepPolling.current` (a ref it owns, created before the
 * query: its poll callback runs outside render), which this keeps false once the version is missing.
 */
export function useVersionLookup(
  versionId: string | null,
  query: { data?: unknown; error: unknown; errorUpdateCount: number },
  keepPolling: RefObject<boolean>,
  creatingJobId: string | null = null,
) {
  const pendingJob = usePendingVersions((s) => (versionId ? (s.jobs[versionId] ?? null) : null));
  const notFound = !query.data && isMissing(query.error) && query.error.status === 404;
  const jobId = creatingJobId ?? pendingJob;
  const job = useJob(notFound ? jobId : null, true);
  const jobRunning = Boolean(jobId) && (!job.data || !TERMINAL.has(job.data.status)) && !isMissing(job.error);
  const creating = notFound && jobRunning;
  // the job that was to create it failed or was cancelled: that is the news, not "does not exist"
  const jobEnded = notFound && Boolean(jobId) && ["failed", "cancelled"].includes(String(job.data?.status));
  const invalid = !query.data && isMissing(query.error) && query.error.status === 422;
  const missing = invalid || (notFound && !creating && !jobEnded && query.errorUpdateCount >= MISSING_AFTER);
  keepPolling.current = !missing && !jobEnded;
  return { creating, missing, jobEnded };
}
