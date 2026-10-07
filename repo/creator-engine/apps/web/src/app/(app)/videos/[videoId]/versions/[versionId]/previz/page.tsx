"use client";
/**
 * Previz review (§31 Create step 10): while planning runs it follows the plan job; once the version
 * is `previz_ready` it shows the script, storyboard, intent, performance preview, predicted
 * coverage, memory, repetition / contradiction / fact-check findings, duration and cost, and the
 * actions Approve and Regenerate plan.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use, useRef } from "react";

import { PageHeader } from "@/components/app-shell";
import { FindingsList } from "@/components/findings";
import { MissingVersion, useVersionLookup } from "@/components/missing-version";
import { PerformanceLane } from "@/components/performance-lane";
import { RoleNote } from "@/components/role-note";
import { ClaimLedger } from "@/components/research-panels";
import { ApprovePanel, CoveragePanel, IntentPanel, ReplanPanel, ScriptView, Storyboard } from "@/components/plan";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, LoadError, Progress, Skeleton } from "@/components/ui/misc";
import type { PlanReport, VideoSpec } from "@/lib/api";
import type { CoverageEntry } from "@/lib/coverage";
import { humanize, seconds, usd } from "@/lib/format";
import { buildLane, trajectorySentence } from "@/lib/performance";
import { isMissing, useIntent, useJob, usePreviz, useSnapshots, useVersion } from "@/lib/queries";

const WAITING = new Set(["planned", "previz_running"]);

function Waiting({ jobId, state }: { jobId: string | null; state: string | null }) {
  const job = useJob(jobId, true);
  const failed = job.data?.status === "failed";
  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-6">
        {failed ? (
          <Alert tone="danger">
            Planning failed: {String((job.data?.error as { message?: string } | null)?.message ?? "see the job")}
          </Alert>
        ) : (
          <>
            <p className="text-sm" aria-live="polite">
              {state === "previz_running" || state === "planned"
                ? "Previz is running: voice, alignment, plates and keyframes…"
                : "The Director is planning: brief, strategy, script, scenes, acting, intent…"}
            </p>
            {job.data ? <Progress value={job.data.progress} label="Planning progress" /> : <Skeleton className="h-2" />}
          </>
        )}
        {jobId ? (
          <Link className="text-sm text-blue-800 underline" href={`/jobs/${jobId}`}>
            Job details
          </Link>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Review({ videoId, versionId }: { videoId: string; versionId: string }) {
  const params = useSearchParams();
  const keepPolling = useRef(true);
  const version = useVersion(versionId, (v) => (v ? WAITING.has(v.state) : keepPolling.current));
  // a link from Create or Regenerate plan carries the plan job: while it runs, a 404 means "not yet"
  const lookup = useVersionLookup(versionId, version, keepPolling, params.get("job"));
  const state = version.data?.state ?? null;
  const waiting = !state || WAITING.has(state);
  const previz = usePreviz(state ? versionId : null, waiting);
  const intent = useIntent(state ? versionId : null);
  const snapshots = useSnapshots(state ? versionId : null);
  const notPlanned = !version.data;

  if (lookup.missing || (version.data && version.data.video_id !== videoId)) {
    return (
      <>
        <PageHeader title="Previz review" />
        <MissingVersion videoId={videoId} />
      </>
    );
  }
  if (version.error && !isMissing(version.error)) {
    return <LoadError what="this version" error={version.error} onRetry={() => void version.refetch()} />;
  }
  if (notPlanned || (state && WAITING.has(state) && previz.data?.state !== "previz_ready")) {
    return (
      <>
        <PageHeader title="Planning" description="This page updates by itself." />
        <Waiting jobId={params.get("job")} state={previz.data?.state ?? state} />
      </>
    );
  }
  if (!version.data || !previz.data) return <Skeleton className="h-64" />;
  const spec = version.data.spec as unknown as VideoSpec;
  const report = previz.data.plan_report as unknown as PlanReport | null;
  const ready = previz.data.state === "previz_ready";
  const findings = report?.findings ?? [];
  const lane = report
    ? buildLane(version.data.spec, report.state_timings ?? [], report.event_timings ?? [], report.estimated_duration_s)
    : null;
  const entries = (report?.predicted_coverage?.entries ?? []) as unknown as CoverageEntry[];
  const items = snapshots.data?.flatMap((s) => s.items as Record<string, unknown>[]) ?? [];

  return (
    <>
      <PageHeader
        title={spec.meta.title}
        description={
          <span className="flex items-center gap-2">
            <StateBadge state={previz.data.state} /> {humanize(spec.meta.mode)} · version {version.data.number}
            {report?.planner === "template" ? (
              <Badge variant="warning" title="No LLM answer matched this input; a labeled template planned it">
                template plan
              </Badge>
            ) : null}
          </span>
        }
      />
      <RoleNote className="mb-4" />
      {previz.data.state === "failed" ? <Alert tone="danger">Previz failed; regenerate the plan.</Alert> : null}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="col-span-2 flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Script</CardTitle>
            </CardHeader>
            <CardContent>
              <ScriptView spec={spec} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Storyboard</CardTitle>
            </CardHeader>
            <CardContent>
              <Storyboard versionId={versionId} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Intent summary</CardTitle>
            </CardHeader>
            <CardContent>
              <IntentPanel intent={intent.data} />
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Performance timeline</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-2">
              {lane ? (
                <>
                  <p className="text-sm" data-testid="trajectory">
                    {trajectorySentence(lane) || "No acting states."}
                  </p>
                  <PerformanceLane lane={lane} measured={report?.timing_source === "measured"} />
                </>
              ) : (
                <Empty>No timings yet.</Empty>
              )}
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Predicted coverage</CardTitle>
            </CardHeader>
            <CardContent>
              {entries.length ? <CoveragePanel spec={spec} entries={entries} /> : <Empty>No behavior items.</Empty>}
            </CardContent>
          </Card>
        </div>
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Duration and cost</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-1 text-sm">
                <dt className="text-slate-600">{report?.timing_source === "measured" ? "Measured" : "Estimated"}</dt>
                <dd data-testid="duration">{seconds(report?.estimated_duration_s)}</dd>
                <dt className="text-slate-600">Target</dt>
                <dd>{seconds(report?.target_duration_s)}</dd>
                <dt className="text-slate-600">Generation cost</dt>
                <dd>{usd(previz.data.cost_estimate_usd)}</dd>
              </dl>
            </CardContent>
          </Card>
          <ApprovePanel
            versionId={versionId}
            videoId={videoId}
            blocking={previz.data.blocking}
            disabled={!ready}
            state={previz.data.state}
          />
          <ReplanPanel versionId={versionId} videoId={videoId} disabled={!ready && previz.data.state !== "failed"} />
          <ClaimLedger versionId={versionId} />
          <Card>
            <CardHeader>
              <CardTitle>Findings</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <section>
                <h3 className="mb-1 text-sm font-medium">Repetition</h3>
                <FindingsList
                  findings={findings.filter((f) => f.kind === "repetition")}
                  empty="Nothing repeats recent videos."
                />
              </section>
              <section>
                <h3 className="mb-1 text-sm font-medium">Contradictions</h3>
                <FindingsList
                  findings={findings.filter((f) => f.kind === "contradiction")}
                  empty="Nothing contradicts the creator's canon or memory."
                />
              </section>
              <section>
                <h3 className="mb-1 text-sm font-medium">Fact check</h3>
                <FindingsList findings={findings.filter((f) => f.kind === "fact_check")} empty="No flagged claims." />
              </section>
              <section>
                <h3 className="mb-1 text-sm font-medium">Other</h3>
                <FindingsList
                  findings={findings.filter((f) => !["repetition", "contradiction", "fact_check"].includes(f.kind))}
                  empty="None."
                />
              </section>
            </CardContent>
          </Card>
          <Card>
            <CardHeader>
              <CardTitle>Memory used</CardTitle>
            </CardHeader>
            <CardContent>
              {items.length ? (
                <ul className="flex flex-col gap-1 text-sm">
                  {items.map((item) => (
                    <li key={String(item.item_id)}>
                      {String(item.text || humanize(String(item.kind)))}
                      {item.pinned ? (
                        <Badge variant="outline" className="ml-1">
                          pinned
                        </Badge>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>No memory items were used.</Empty>
              )}
            </CardContent>
          </Card>
          {report?.assumptions?.length ? (
            <Card>
              <CardHeader>
                <CardTitle>Assumptions</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="list-disc pl-5 text-sm">
                  {report.assumptions.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ) : null}
        </div>
      </div>
    </>
  );
}

export default function PrevizPage({ params }: { params: Promise<{ videoId: string; versionId: string }> }) {
  const { videoId, versionId } = use(params);
  return (
    <Suspense fallback={<Skeleton className="h-64" />}>
      <Review videoId={videoId} versionId={versionId} />
    </Suspense>
  );
}
