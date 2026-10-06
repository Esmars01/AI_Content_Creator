"use client";
/**
 * Video Studio (§31): player, job progress, scene list with coverage and world badges, the
 * Performance lane, the NL edit panel with proposal cards, the Intent / Performance / Coverage /
 * Creator & World panels (Simple → Advanced, with the Advanced performance and intent editors),
 * locks, shots and takes, and the versions tree with restore, branch, resume and compare.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use } from "react";

import { PageHeader, useAdvanced } from "@/components/app-shell";
import { SceneCoverageBadge } from "@/components/coverage";
import { EditPanel } from "@/components/edit-panel";
import { CaptionsPanel, ExportPanel, PackagingPanel } from "@/components/export-panels";
import { ClaimLedger } from "@/components/research-panels";
import { IntentEditor, PerformanceEditor } from "@/components/editors";
import { PerformanceLane } from "@/components/performance-lane";
import { CritiquePanel, QCReportPanel, VersionConsistency } from "@/components/qc-panels";
import { Player } from "@/components/player";
import { CoveragePanel, IntentPanel, scenesOf, segmentScenes } from "@/components/plan";
import { StateBadge } from "@/components/state-badge";
import { LocksPanel, TakesGallery, VersionsPanel } from "@/components/studio-panels";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import type { PlanReport, VideoSpec } from "@/lib/api";
import { bySceneKey, type CoverageEntry } from "@/lib/coverage";
import { humanize, seconds, when } from "@/lib/format";
import { buildLane, trajectorySentence } from "@/lib/performance";
import { ApiError } from "@/lib/api";
import { useCoverage, useIntent, useJobs, usePreviz, useVersion, useVersions, useVideo } from "@/lib/queries";

const BUILDING = new Set(["approved", "generating"]);
const PREVIZ = new Set(["planned", "previz_running"]);

function ActiveJobs({ versionIds }: { versionIds: string[] }) {
  const running = useJobs("running", true);
  const queued = useJobs("queued", true);
  const jobs = [...(running.data?.items ?? []), ...(queued.data?.items ?? [])].filter(
    (job) => job.video_version_id && versionIds.includes(job.video_version_id),
  );
  if (!jobs.length) return null;
  return (
    <Card>
      <CardContent className="flex flex-col gap-2 py-3" aria-live="polite">
        {jobs.map((job) => (
          <div key={job.id} className="flex flex-col gap-1" data-testid="job-progress">
            <div className="flex items-center justify-between text-sm">
              <Link className="text-blue-800 hover:underline" href={`/jobs/${job.id}`}>
                {humanize(job.kind)}
              </Link>
              <StateBadge state={job.status} />
            </div>
            <Progress value={job.progress} label={`${job.kind} progress`} />
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function Studio({ videoId }: { videoId: string }) {
  const params = useSearchParams();
  const advanced = useAdvanced();
  const video = useVideo(videoId);
  const versions = useVersions(videoId);
  const versionId = params.get("version") ?? video.data?.current_version_id ?? null;
  const version = useVersion(versionId, (v) => !v || BUILDING.has(v.state) || PREVIZ.has(v.state));
  const previz = usePreviz(versionId);
  const intent = useIntent(versionId);
  const state = version.data?.state ?? "";
  const built = ["ready", "partial", "needs_review", "generating"].includes(state);
  const coverage = useCoverage(built ? versionId : null);

  if (video.isLoading) return <Skeleton className="h-64" />;
  if (!video.data) return <Alert tone="danger">This video does not exist.</Alert>;
  if (!versionId) {
    return (
      <>
        <PageHeader title={video.data.title || "Untitled"} />
        <Empty>{video.data.planning ? "Planning is under way…" : "No version yet."}</Empty>
      </>
    );
  }
  if (version.error instanceof ApiError && version.error.status === 404) {
    // a derived version appears once its edit is applied (ApplyEditWorkflow)
    return (
      <>
        <PageHeader title={video.data.title || "Untitled"} />
        <Alert tone="info" aria-live="polite">
          Creating the new version…
        </Alert>
      </>
    );
  }
  if (!version.data) return <Skeleton className="h-64" />;
  const spec = version.data.spec as unknown as VideoSpec;
  const report = previz.data?.plan_report as unknown as PlanReport | null | undefined;
  const lane = report
    ? buildLane(version.data.spec, report.state_timings ?? [], report.event_timings ?? [], report.estimated_duration_s)
    : null;
  const observed = coverage.data?.entries as CoverageEntry[] | undefined;
  const predicted = (report?.predicted_coverage?.entries ?? []) as unknown as CoverageEntry[];
  const entries = observed?.length ? observed : predicted;
  const grouped = bySceneKey(entries, segmentScenes(spec));
  const allVersions = versions.data?.items ?? [];

  return (
    <>
      <PageHeader
        title={spec.meta.title || video.data.title}
        description={
          <span className="flex items-center gap-2">
            <StateBadge state={state} /> version {version.data.number} · {humanize(spec.meta.mode)}
          </span>
        }
        actions={
          <div className="flex gap-2">
            <Button variant="outline" asChild>
              <Link href={`/videos/${videoId}/versions/${versionId}/previz`}>Previz review</Link>
            </Button>
            <Button variant="outline" asChild>
              <Link href={`/developer?version=${versionId}`}>Developer</Link>
            </Button>
          </div>
        }
      />
      <div className="grid grid-cols-[minmax(0,22rem)_1fr] gap-4">
        <div className="flex flex-col gap-4">
          <Player versionId={versionId} state={state} />
          <ActiveJobs versionIds={[versionId]} />
          <VersionsPanel videoId={videoId} versions={allVersions} currentId={versionId} />
          <LocksPanel key={`locks-${versionId}`} spec={version.data.spec} versionId={versionId} />
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          {state === "previz_ready" ? (
            <Alert tone="info">
              This version is waiting for approval.{" "}
              <Link className="underline" href={`/videos/${videoId}/versions/${versionId}/previz`}>
                Review the previz
              </Link>
              .
            </Alert>
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>Performance</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {lane ? (
                <>
                  <p className="text-sm">{trajectorySentence(lane)}</p>
                  <PerformanceLane lane={lane} measured={report?.timing_source === "measured"} />
                  {advanced ? (
                    <Table>
                      <thead>
                        <tr>
                          <Th>State</Th>
                          <Th>Displayed / felt</Th>
                          <Th>Time</Th>
                          <Th>Transition</Th>
                        </tr>
                      </thead>
                      <tbody>
                        {lane.bands.map((band) => (
                          <tr key={band.key}>
                            <Td className="font-mono text-xs">{band.key}</Td>
                            <Td>
                              {humanize(band.label)}
                              {band.felt ? ` / ${humanize(band.felt)}` : ""}
                              {band.masking ? (
                                <Badge variant="outline" className="ml-1">
                                  masking
                                </Badge>
                              ) : null}
                            </Td>
                            <Td>
                              {seconds(band.startS)}–{seconds(band.endS)}
                            </Td>
                            <Td>
                              {[band.transition, band.trigger]
                                .filter(Boolean)
                                .map((t) => humanize(t))
                                .join(" · ") || "—"}
                            </Td>
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  ) : null}
                </>
              ) : (
                <Empty>No performance timings.</Empty>
              )}
            </CardContent>
          </Card>
          <EditPanel
            key={`edit-${versionId}`}
            versionId={versionId}
            videoId={videoId}
            sceneKeys={scenesOf(spec).map((scene) => scene.key)}
          />
          {advanced ? (
            <PerformanceEditor key={`perf-${versionId}`} spec={version.data.spec} versionId={versionId} />
          ) : null}
          <Card>
            <CardHeader>
              <CardTitle>Scenes</CardTitle>
            </CardHeader>
            <CardContent>
              <Table>
                <thead>
                  <tr>
                    <Th>Scene</Th>
                    <Th>Purpose</Th>
                    <Th>World</Th>
                    <Th>Behavior coverage</Th>
                  </tr>
                </thead>
                <tbody>
                  {scenesOf(spec).map((scene) => (
                    <tr key={scene.key} data-testid="scene-row">
                      <Td className="font-mono text-xs">{scene.key}</Td>
                      <Td>{humanize(scene.purpose)}</Td>
                      <Td>
                        {scene.world ? (
                          <Badge variant="outline" title={scene.world.world_version_id}>
                            {humanize(scene.world.camera_position_key)} · {humanize(scene.world.time_of_day)}
                          </Badge>
                        ) : (
                          "—"
                        )}
                      </Td>
                      <Td>
                        <SceneCoverageBadge entries={grouped[scene.key] ?? []} />
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <p className="mt-2 text-xs text-slate-600">
                {observed?.length
                  ? "Coverage after generation: delivered counts only behaviors the analyzers confirmed."
                  : "Predicted coverage: nothing is delivered until the video is generated and observed."}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Intent</CardTitle>
            </CardHeader>
            <CardContent>
              <IntentPanel intent={intent.data} />
            </CardContent>
          </Card>
          {advanced ? (
            <IntentEditor key={`intent-${versionId}`} spec={version.data.spec} versionId={versionId} />
          ) : null}
          <TakesGallery spec={version.data.spec} versionId={versionId} />
          {advanced && entries.length ? (
            <Card>
              <CardHeader>
                <CardTitle>Behavior coverage</CardTitle>
              </CardHeader>
              <CardContent>
                <CoveragePanel spec={spec} entries={entries} />
              </CardContent>
            </Card>
          ) : null}
          <ClaimLedger versionId={versionId} />
          {built ? <CaptionsPanel versionId={versionId} videoId={videoId} /> : null}
          {built ? <PackagingPanel versionId={versionId} targets={spec.meta.platform_targets ?? []} /> : null}
          {built ? <ExportPanel versionId={versionId} versionState={state} /> : null}
          {built ? <QCReportPanel versionId={versionId} /> : null}
          {built ? <CritiquePanel versionId={versionId} /> : null}
          {built ? <VersionConsistency versionId={versionId} /> : null}
          <Card>
            <CardHeader>
              <CardTitle>Creator and world</CardTitle>
            </CardHeader>
            <CardContent className="text-sm">
              <ul>
                {(spec.cast ?? []).map((member) => (
                  <li key={member.key}>
                    {member.key} ({humanize(member.role)}) · creator version{" "}
                    <code className="text-xs">{member.creator_version_id}</code>
                  </li>
                ))}
              </ul>
              <p className="mt-1 text-xs text-slate-600">Created {when(version.data.created_at)}</p>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  );
}

export default function VideoStudioPage({ params }: { params: Promise<{ videoId: string }> }) {
  const { videoId } = use(params);
  return (
    <Suspense fallback={<Skeleton className="h-64" />}>
      <Studio videoId={videoId} />
    </Suspense>
  );
}
