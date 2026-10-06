"use client";
/** Models (§31, Phase 11): the registry with status, validation and promotion basis; each model's
 * benchmarks, and for platform admins the benchmark runner and the blind pairwise rating. A model
 * is promoted only on a passed benchmark (`bench_passed`); GPU engines here are `untested_on_gpu`. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { Fragment, useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { ErrorNote, JobLine, useStudioJob } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { useMe } from "@/lib/queries";

function ModelDetail({ modelId, admin }: { modelId: string; admin: boolean }) {
  const client = useQueryClient();
  const detail = useQuery({
    queryKey: ["model", modelId],
    queryFn: () => unwrap(api.GET("/v1/models/{model_id}", { params: { path: { model_id: modelId } } })),
  });
  const job = useStudioJob([["model", modelId]]);
  const run = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/admin/models/{model_id}:benchmark", { params: { path: { model_id: modelId } } })),
    onSuccess: (data) => {
      job.setJobId(data.job_id);
      void client.invalidateQueries({ queryKey: ["model", modelId] });
    },
  });
  if (detail.isLoading) return <Skeleton className="h-24" />;
  const model = detail.data;
  if (!model) return null;
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap gap-2">
        <span>Capabilities: {model.capabilities.join(", ")}</span>
        <span>
          Revision <code className="text-xs">{model.revision.slice(0, 12)}</code>
        </span>
      </div>
      {admin ? (
        <div className="flex items-center gap-2">
          <Button onClick={() => run.mutate()} disabled={run.isPending}>
            Run benchmark
          </Button>
          <JobLine status={job.status} job={job.job} />
        </div>
      ) : null}
      <ErrorNote error={run.error} />
      {(model.benchmarks ?? []).length === 0 ? <Empty>No benchmark yet.</Empty> : null}
      <ul className="space-y-1">
        {(model.benchmarks ?? []).map((b) => {
          const scores = (b.human_scores ?? {}) as Record<string, unknown>;
          const status = (scores.status ?? {}) as Record<string, unknown>;
          return (
            <li key={b.id} className="flex flex-wrap items-center gap-2">
              <Badge variant={b.verdict === "pass" ? "success" : b.verdict === "fail" ? "danger" : "info"}>
                {b.verdict}
              </Badge>
              <span>
                {b.eval_set_version} · {when(b.created_at)} · checks{" "}
                {typeof status.check_pass_rate === "number" ? `${Math.round(status.check_pass_rate * 100)}%` : "—"} ·{" "}
                {String(scores.rated ?? 0)}/{String(scores.pairs ?? 0)} pairs rated
                {(b.metrics as Record<string, unknown>).mock ? " · mock engines" : ""}
              </span>
              {admin && Number(scores.pairs ?? 0) > 0 && b.verdict === "pending" ? (
                <Link className="underline" href={`/models/benchmarks/${b.id}`}>
                  Rate pairs
                </Link>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export default function ModelsPage() {
  const me = useMe();
  const admin = Boolean(me.data?.user.is_platform_admin);
  const models = useQuery({ queryKey: ["models"], queryFn: () => unwrap(api.GET("/v1/models")) });
  const [open, setOpen] = useState<string | null>(null);
  return (
    <>
      <PageHeader
        title="Models"
        description="Engines, their validation evidence and benchmarks. Promotion needs a passed benchmark."
      />
      <ErrorNote error={models.error} />
      {models.isLoading ? <Skeleton className="h-64" /> : null}
      <Card>
        <CardHeader>
          <CardTitle>Registry</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <thead>
              <tr>
                <Th>Model</Th>
                <Th>Status</Th>
                <Th>Validation</Th>
                <Th>License</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {(models.data ?? []).map((m) => (
                <Fragment key={m.id}>
                  <tr>
                    <Td>
                      {m.display_name || m.model_key}
                      <div className="text-xs text-slate-600">{m.plugin_key}</div>
                    </Td>
                    <Td>
                      <Badge
                        variant={m.status === "production" ? "success" : m.status === "disabled" ? "danger" : "muted"}
                      >
                        {humanize(m.status)}
                      </Badge>
                      {m.smoke_promoted ? <Badge variant="warning">smoke-promoted</Badge> : null}
                    </Td>
                    <Td>{humanize(m.validation)}</Td>
                    <Td>{m.license.name}</Td>
                    <Td>
                      <Button variant="outline" onClick={() => setOpen(open === m.id ? null : m.id)}>
                        {open === m.id ? "Hide" : "Details"}
                      </Button>
                    </Td>
                  </tr>
                  {open === m.id ? (
                    <tr key={`${m.id}-detail`}>
                      <Td colSpan={5}>
                        <ModelDetail modelId={m.id} admin={admin} />
                      </Td>
                    </tr>
                  ) : null}
                </Fragment>
              ))}
            </tbody>
          </Table>
        </CardContent>
      </Card>
    </>
  );
}
