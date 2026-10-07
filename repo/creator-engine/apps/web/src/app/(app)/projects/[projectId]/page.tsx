"use client";
import Link from "next/link";
import { use } from "react";

import { PageHeader } from "@/components/app-shell";
import { ProjectBrandKit } from "@/components/brand-kits";
import { SourcesPanel } from "@/components/research-panels";
import { RoleNote } from "@/components/role-note";
import { StateBadge } from "@/components/state-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, LoadError, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { humanize, when } from "@/lib/format";
import { isMissing, useProject, useProjectVideos } from "@/lib/queries";

export default function ProjectPage({ params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = use(params);
  const project = useProject(projectId);
  const videos = useProjectVideos(projectId);
  if (project.isLoading) return <Skeleton className="h-64" />;
  if (project.error && !isMissing(project.error)) {
    return <LoadError what="this project" error={project.error} onRetry={() => void project.refetch()} />;
  }
  if (!project.data) return <Empty>This project does not exist.</Empty>;
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={project.data.name}
        description={project.data.description || undefined}
        actions={
          <Button asChild>
            <Link href={`/create?project=${projectId}`}>New video</Link>
          </Button>
        }
      />
      <RoleNote />
      <ProjectBrandKit projectId={projectId} current={project.data.brand_kit_id ?? null} />
      <Card>
        <CardContent>
          {videos.isLoading ? (
            <Skeleton className="h-24" />
          ) : videos.error ? (
            <LoadError what="this project's videos" error={videos.error} onRetry={() => void videos.refetch()} />
          ) : videos.data?.items.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Video</Th>
                  <Th>Mode</Th>
                  <Th>State</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {videos.data.items.map((video) => (
                  <tr key={video.id}>
                    <Td>
                      <Link className="text-blue-800 hover:underline" href={`/videos/${video.id}`}>
                        {video.title || "Untitled"}
                      </Link>
                    </Td>
                    <Td>{humanize(video.mode)}</Td>
                    <Td>
                      {video.planning ? (
                        <StateBadge state="planning" />
                      ) : (
                        <StateBadge state={video.current_version_state} />
                      )}
                    </Td>
                    <Td className="text-xs">{when(video.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>No videos in this project yet.</Empty>
          )}
        </CardContent>
      </Card>
      <SourcesPanel projectId={projectId} />
    </div>
  );
}
