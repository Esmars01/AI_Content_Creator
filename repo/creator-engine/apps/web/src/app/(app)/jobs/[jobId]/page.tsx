"use client";
import { use } from "react";

import { PageHeader } from "@/components/app-shell";
import { CancelJobButton } from "@/components/cancel-job";
import { JsonViewer } from "@/components/json-viewer";
import { StateBadge } from "@/components/state-badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, LoadError, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { humanize, usd } from "@/lib/format";
import { isMissing, useJob } from "@/lib/queries";

export default function JobPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = use(params);
  const job = useJob(jobId, true);
  if (job.isLoading) return <Skeleton className="h-64" />;
  if (job.error && !isMissing(job.error)) {
    return <LoadError what="this job" error={job.error} onRetry={() => void job.refetch()} />;
  }
  if (!job.data) return <Empty>This job does not exist.</Empty>;
  const data = job.data;
  return (
    <>
      <PageHeader
        title={humanize(data.kind)}
        description={
          <span className="flex items-center gap-2">
            <StateBadge state={data.status} /> {data.target_type} <code className="text-xs">{data.target_id}</code>
          </span>
        }
      />
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-3">
          <div className="flex-1">
            <Progress value={data.progress} label="Job progress" />
          </div>
          <CancelJobButton job={data} />
        </div>
        {data.error ? (
          <Alert tone="danger">
            {String((data.error as { message?: string }).message ?? JSON.stringify(data.error))}
          </Alert>
        ) : null}
        <Card>
          <CardHeader>
            <CardTitle>Nodes ({data.nodes.length})</CardTitle>
          </CardHeader>
          <CardContent>
            {data.nodes.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Node</Th>
                    <Th>Status</Th>
                    <Th>Route</Th>
                    <Th>Attempts</Th>
                    <Th>Cost</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.nodes.map((node) => (
                    <tr key={node.node_key}>
                      <Td className="font-mono text-xs">{node.node_key}</Td>
                      <Td>
                        <StateBadge state={node.status} />
                      </Td>
                      <Td className="text-xs">
                        {node.route
                          ? `${String(node.route.adapter_id ?? "")} ${String(node.route.model_id ?? "")}`
                          : "—"}
                      </Td>
                      <Td>{node.attempts}</Td>
                      <Td>{usd((node.attempt_history ?? []).reduce((sum, a) => sum + Number(a.cost_usd || 0), 0))}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <Empty>This job runs no build nodes (planning runs the Director in one activity).</Empty>
            )}
          </CardContent>
        </Card>
        <JsonViewer value={data} label="Job record" />
      </div>
    </>
  );
}
