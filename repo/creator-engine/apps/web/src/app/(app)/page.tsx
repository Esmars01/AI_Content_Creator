"use client";
import { useQueries } from "@tanstack/react-query";
import Link from "next/link";

import { PageHeader } from "@/components/app-shell";
import { CancelJobButton } from "@/components/cancel-job";
import { StateBadge } from "@/components/state-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, LoadError, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { keys, useCreators, useJobs, useMe, useProjects } from "@/lib/queries";

/** Mock wording only where mock GPU workers are actually registered (never in production). */
function SpendNote() {
  const environment = useMe().data?.environment;
  if (!environment) return null;
  if (environment.mock_gpu) return <>Mock mode: simulated GPU workers, no GPU is used and nothing is spent.</>;
  return <>GPU work runs on the providers an administrator has enabled, within their budgets.</>;
}

function RecentVideos() {
  const projects = useProjects();
  const recent = (projects.data?.items ?? []).slice(0, 8);
  const videos = useQueries({
    queries: recent.map((project) => ({
      queryKey: keys.projectVideos(project.id),
      queryFn: () =>
        unwrap(
          api.GET("/v1/projects/{project_id}/videos", {
            params: { path: { project_id: project.id }, query: { limit: 5 } },
          }),
        ),
    })),
  });
  if (projects.isLoading) return <Skeleton className="h-24" />;
  const failed = projects.error ?? videos.find((v) => v.error)?.error;
  if (failed) return <LoadError what="recent videos" error={failed} onRetry={() => void projects.refetch()} />;
  const rows = recent.flatMap((project, i) => (videos[i]?.data?.items ?? []).map((video) => ({ project, video })));
  rows.sort((a, b) => b.video.created_at.localeCompare(a.video.created_at));
  if (!rows.length) {
    return (
      <Empty>
        No videos yet.{" "}
        <Link className="text-blue-800 underline" href="/create">
          Create one
        </Link>{" "}
        from an idea or a script.
      </Empty>
    );
  }
  return (
    <Table>
      <thead>
        <tr>
          <Th>Video</Th>
          <Th>Project</Th>
          <Th>State</Th>
          <Th>Created</Th>
        </tr>
      </thead>
      <tbody>
        {rows.slice(0, 10).map(({ project, video }) => (
          <tr key={video.id}>
            <Td>
              <Link className="text-blue-800 hover:underline" href={`/videos/${video.id}`}>
                {video.title || "Untitled"}
              </Link>
              <div className="text-xs text-slate-600">{humanize(video.mode)}</div>
            </Td>
            <Td>{project.name}</Td>
            <Td>
              {video.planning ? <StateBadge state="planning" /> : <StateBadge state={video.current_version_state} />}
            </Td>
            <Td className="text-xs">{when(video.created_at)}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function RunningJobs() {
  const running = useJobs("running", true);
  const queued = useJobs("queued", true);
  const jobs = [...(running.data?.items ?? []), ...(queued.data?.items ?? [])];
  if (running.isLoading) return <Skeleton className="h-16" />;
  if (!jobs.length) return <Empty>No jobs are running.</Empty>;
  return (
    <ul className="flex flex-col gap-3">
      {jobs.map((job) => (
        <li key={job.id} className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center justify-between gap-1 text-sm">
            <Link className="text-blue-800 hover:underline" href={`/jobs/${job.id}`}>
              {humanize(job.kind)}
            </Link>
            <span className="flex items-center gap-2">
              <StateBadge state={job.status} />
              <CancelJobButton job={job} />
            </span>
          </div>
          <Progress value={job.progress} label={`${job.kind} progress`} />
        </li>
      ))}
    </ul>
  );
}

function MemoryConflicts() {
  const creators = useCreators();
  const list = creators.data?.items ?? [];
  const memory = useQueries({
    queries: list.map((creator) => ({
      queryKey: keys.memory(creator.id),
      queryFn: () =>
        unwrap(
          api.GET("/v1/creators/{creator_id}/memory", {
            params: { path: { creator_id: creator.id }, query: { limit: 200 } },
          }),
        ),
    })),
  });
  const conflicts = list
    .map((creator, i) => ({
      creator,
      count: (memory[i]?.data?.items ?? []).filter((item) => item.conflict_state === "unresolved").length,
    }))
    .filter((row) => row.count > 0);
  if (creators.isLoading) return <Skeleton className="h-10" />;
  if (!conflicts.length) return <Empty>No unresolved memory conflicts.</Empty>;
  return (
    <ul className="text-sm">
      {conflicts.map(({ creator, count }) => (
        <li key={creator.id}>
          <Link className="text-blue-800 hover:underline" href={`/creators/${creator.id}`}>
            {creator.name}
          </Link>
          : {count} unresolved
        </li>
      ))}
    </ul>
  );
}

export default function DashboardPage() {
  return (
    <>
      <PageHeader
        title="Dashboard"
        actions={
          <Button asChild>
            <Link href="/create">Create a video</Link>
          </Button>
        }
      />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
        <Card className="md:col-span-2">
          <CardHeader>
            <CardTitle>Recent videos</CardTitle>
          </CardHeader>
          <CardContent>
            <RecentVideos />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Running jobs</CardTitle>
            <CardDescription>Live over server-sent events.</CardDescription>
          </CardHeader>
          <CardContent>
            <RunningJobs />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Memory conflicts</CardTitle>
          </CardHeader>
          <CardContent>
            <MemoryConflicts />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Spend and fleet</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-slate-700">
            <SpendNote /> Budgets, spend and the worker fleet are on the{" "}
            <Link className="text-blue-800 underline" href="/gpu">
              GPU
            </Link>{" "}
            page.
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>QC and consistency flags</CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-slate-700">
            Each video&apos;s QC report, creator consistency and behavior coverage are in its studio; takes waiting for
            a human verdict are in the{" "}
            <Link className="text-blue-800 underline" href="/ratings">
              rating queue
            </Link>
            .
          </CardContent>
        </Card>
      </div>
    </>
  );
}
