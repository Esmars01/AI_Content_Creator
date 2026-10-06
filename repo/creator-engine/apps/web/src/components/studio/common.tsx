"use client";
/** Shared pieces of the studios (Phase 10): asset images, artifact audio, and studio jobs that run in
 * the background and refresh what they change when they finish. */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Alert, Skeleton } from "@/components/ui/misc";
import { api, ApiError, unwrap } from "@/lib/api";
import { humanize } from "@/lib/format";

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
export function useStudioJob(invalidate: readonly (readonly unknown[])[]) {
  const client = useQueryClient();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useQuery({
    queryKey: ["jobs", jobId ?? ""],
    enabled: Boolean(jobId),
    queryFn: () => unwrap(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: jobId ?? "" } } })),
    refetchInterval: (query) => {
      const status = (query.state.data as { status?: string } | undefined)?.status;
      return status && ["succeeded", "failed", "cancelled", "partial"].includes(status) ? false : 1500;
    },
  });
  const status = (job.data as { status?: string } | undefined)?.status ?? null;
  useEffect(() => {
    if (status && ["succeeded", "failed", "cancelled", "partial"].includes(status)) {
      for (const key of invalidate) void client.invalidateQueries({ queryKey: key });
    }
  }, [status, client, invalidate]);
  return { jobId, setJobId, job: job.data as Record<string, unknown> | undefined, status };
}

export function JobLine({ status, job }: { status: string | null; job?: Record<string, unknown> }) {
  if (!status) return null;
  const error = job?.error as { message?: string } | undefined;
  const tone = status === "succeeded" ? "success" : status === "failed" ? "danger" : "info";
  return (
    <Alert tone={tone}>
      Job {humanize(status)}
      {status === "running" || status === "queued" ? "…" : ""}
      {error?.message ? ` — ${error.message}` : ""}
    </Alert>
  );
}
