"use client";
/** Shared pieces of the studios (Phase 10): asset images, artifact audio, and studio jobs that run in
 * the background and refresh what they change when they finish. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Alert, Skeleton } from "@/components/ui/misc";
import { api, ApiError, unwrap } from "@/lib/api";
import { humanize } from "@/lib/format";
import { stageText } from "@/lib/gpu";

export function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const issues = (error.problem?.issues ?? []) as { message?: string; path?: string }[];
    const detail = issues.map((i) => [i.path, i.message].filter(Boolean).join(": ")).join("; ");
    return [error.problem?.detail ?? error.message, detail].filter(Boolean).join(" — ");
  }
  return error instanceof Error ? error.message : String(error);
}

export function ErrorNote({ error }: { error: unknown }) {
  return error ? <Alert tone="danger">{errorText(error)}</Alert> : null;
}

export function useAsset(assetId: string | null | undefined) {
  return useQuery({
    queryKey: ["asset", assetId ?? ""],
    enabled: Boolean(assetId),
    queryFn: () => unwrap(api.GET("/v1/assets/{asset_id}", { params: { path: { asset_id: assetId ?? "" } } })),
    staleTime: 600_000, // presigned links last 15 minutes
    retry: false,
  });
}

export function AssetImage({ assetId, alt, className }: { assetId: string; alt: string; className?: string }) {
  const asset = useAsset(assetId);
  if (asset.isLoading) return <Skeleton className={className ?? "h-32 w-32"} />;
  if (!asset.data?.download_url) return <div className="text-xs text-slate-600">image unavailable</div>;
  return <img src={asset.data.download_url} alt={alt} className={className ?? "h-32 w-32 rounded object-cover"} />;
}

export function AssetAudio({ assetId, label }: { assetId: string; label: string }) {
  const asset = useAsset(assetId);
  if (!asset.data?.download_url) return <Skeleton className="h-8 w-64" />;
  return <audio controls src={asset.data.download_url} aria-label={label} className="h-8" />;
}

export function ArtifactAudio({ artifactId, label }: { artifactId: string; label: string }) {
  const link = useQuery({
    queryKey: ["artifact-link", artifactId],
    queryFn: () =>
      unwrap(api.GET("/v1/artifacts/{artifact_id}/download", { params: { path: { artifact_id: artifactId } } })),
    staleTime: 600_000,
  });
  if (!link.data) return <Skeleton className="h-8 w-64" />;
  return <audio controls src={link.data.url} aria-label={label} className="h-8" />;
}

export function MockBadge({ mock }: { mock?: boolean | null }) {
  return mock ? (
    <Badge variant="warning" title="produced by a mock engine: a pipeline check, not a measurement of the creator">
      mock
    </Badge>
  ) : null;
}

/** A studio job: start it, watch it, and refresh `invalidate` when it ends. */
const TERMINAL_JOB = ["succeeded", "failed", "cancelled", "partial"];

export function useStudioJob(invalidate: readonly (readonly unknown[])[]) {
  const client = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useQuery({
    queryKey: ["jobs", jobId ?? ""],
    enabled: Boolean(jobId),
    queryFn: () => unwrap(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: jobId ?? "" } } })),
    refetchInterval: (query) => {
      const status = (query.state.data as { status?: string } | undefined)?.status;
      return status && TERMINAL_JOB.includes(status) ? false : 1500;
    },
  });
  const status = (job.data as { status?: string } | undefined)?.status ?? null;
  // Callers pass a fresh array literal on every render; reading it through a ref and invalidating
  // once per finished job keeps this from re-running after every render (which, with each refetch
  // re-rendering the caller, was an endless refetch loop once the job had finished).
  const latest = useRef(invalidate);
  latest.current = invalidate;
  const handled = useRef<string | null>(null);
  useEffect(() => {
    if (!jobId || !status || !TERMINAL_JOB.includes(status)) return;
    if (handled.current === jobId) return;
    handled.current = jobId;
    for (const key of latest.current) void client.invalidateQueries({ queryKey: key });
  }, [jobId, status, client]);
  return { jobId, setJobId, job: job.data as Record<string, unknown> | undefined, status };
}

export function JobLine({ status, job }: { status: string | null; job?: Record<string, unknown> }) {
  if (!status) return null;
  const error = job?.error as { message?: string } | undefined;
  const tone = status === "succeeded" ? "success" : status === "failed" ? "danger" : "info";
  const active = status === "running" || status === "queued";
  // where its GPU work is (waiting for a GPU, downloading the model, generating…), when it has some
  const nodes = (job?.nodes as { gpu?: Record<string, unknown> | null }[] | undefined) ?? [];
  const stage = typeof job?.stage === "string" ? job.stage : null;
  const node = stage ? nodes.find((n) => n.gpu?.stage === stage) : undefined;
  const detail = active ? (stageText(node?.gpu) ?? (job?.stage_label as string | undefined)) : undefined;
  return (
    <Alert tone={tone}>
      Job {humanize(status)}
      {detail ? `: ${detail}` : ""}
      {active ? "…" : ""}
      {error?.message ? ` — ${error.message}` : ""}
    </Alert>
  );
}
