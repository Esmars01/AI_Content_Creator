"use client";
import Link from "next/link";
import { useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { StateBadge } from "@/components/state-badge";
import { Label, Select } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, LoadError, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { humanize, usd, when } from "@/lib/format";
import { useJobs } from "@/lib/queries";

const STATUSES = ["", "queued", "running", "succeeded", "partial", "failed", "cancelled"];

export default function JobsPage() {
  const [status, setStatus] = useState("");
  const jobs = useJobs(status || null, true);
  return (
    <>
      <PageHeader
        title="Jobs"
        description="Every plan, previz, generation and render is a job (a Temporal workflow)."
      />
      <div className="mb-3 flex items-center gap-2">
        <Label htmlFor="status">Status</Label>
        <Select id="status" className="w-48" value={status} onChange={(e) => setStatus(e.target.value)}>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s ? humanize(s) : "All"}
            </option>
          ))}
        </Select>
      </div>
      <Card>
        <CardContent>
          {jobs.isLoading ? (
            <Skeleton className="h-24" />
          ) : jobs.error ? (
            <LoadError what="jobs" error={jobs.error} onRetry={() => void jobs.refetch()} />
          ) : jobs.data?.items.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Job</Th>
                  <Th>Status</Th>
                  <Th>Progress</Th>
                  <Th>Estimate</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {jobs.data.items.map((job) => (
                  <tr key={job.id}>
                    <Td>
                      <Link className="text-blue-800 hover:underline" href={`/jobs/${job.id}`}>
                        {humanize(job.kind)}
                      </Link>
                      <div className="font-mono text-xs text-slate-600">{job.id}</div>
                    </Td>
                    <Td>
                      <StateBadge state={job.status} />
                    </Td>
                    <Td className="w-48">
                      <Progress value={job.progress} label={`${job.kind} progress`} />
                    </Td>
                    <Td>{usd(job.cost_estimate_usd)}</Td>
                    <Td className="text-xs">{when(job.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>No jobs.</Empty>
          )}
        </CardContent>
      </Card>
    </>
  );
}
