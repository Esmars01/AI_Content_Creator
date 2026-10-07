"use client";
/** The QC report, the Creative Director critique and the version's consistency (Phase 11, §26, §20). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { ErrorNote, JobLine, useStudioJob } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize } from "@/lib/format";
import { keys } from "@/lib/queries";
import { type Json, ladderSteps, metricRows, scoreBars, statusTone, triadRows } from "@/lib/qc";
import { useCan } from "@/lib/roles";

type Tone = "success" | "warning" | "danger" | "info" | "neutral";
const badge = (tone: Tone) => (tone === "neutral" ? "muted" : tone);

export function QCReportPanel({ versionId }: { versionId: string }) {
  const report = useQuery({
    queryKey: keys.qc(versionId),
    queryFn: () => unwrap(api.GET("/v1/versions/{version_id}/qc", { params: { path: { version_id: versionId } } })),
  });
  if (report.isLoading) return <Skeleton className="h-40" />;
  if (report.error) return <ErrorNote error={report.error} />;
  const data = report.data;
  if (!data) return null;
  const shots = data.shots as unknown as { checks: Json; verdict: string; id: string }[];
  const takes = data.takes as unknown as { checks: Json; verdict: string; id: string }[];
  const triad = triadRows(data.coverage as unknown as Json[]);
  const gateShots = (data.gate.shots ?? {}) as { pass?: number; fail?: number };
  return (
    <Card>
      <CardHeader>
        <CardTitle>QC report</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="flex flex-wrap gap-2" aria-label="QC gate summary">
          <Badge variant="success">{gateShots.pass ?? 0} shots passed</Badge>
          {(gateShots.fail ?? 0) > 0 ? <Badge variant="danger">{gateShots.fail} flagged for review</Badge> : null}
          <Badge variant="info">{String(data.gate.retries ?? 0)} retries</Badge>
          {data.flags.map((f) => (
            <Badge key={f} variant="warning">
              {humanize(f)}
            </Badge>
          ))}
        </div>
        {data.state === "needs_review" ? (
          <Alert tone="warning">
            This version needs review: the video rendered with its best attempts, and the flags below say why.
          </Alert>
        ) : null}
        <section aria-label="Shot gates">
          <h3 className="mb-1 font-medium">Shots</h3>
          {shots.length === 0 ? <Empty>No shot gate ran for this version.</Empty> : null}
          {shots.map((shot) => (
            <div key={shot.id} className="mb-2 rounded border border-[var(--color-border)] p-2">
              <div className="flex items-center gap-2">
                <code className="text-xs">{String(shot.checks.shot_key)}</code>
                <Badge variant={shot.verdict === "fail" ? "danger" : "success"}>
                  {shot.verdict === "fail" ? "needs review" : "passed"}
                </Badge>
              </div>
              <ol className="mt-1 list-decimal pl-5">
                {ladderSteps(shot.checks.ladder as Json[]).map((s, i) => (
                  <li key={i}>
                    <Badge variant={badge(s.tone)}>{s.label}</Badge> <span className="text-slate-700">{s.detail}</span>
                  </li>
                ))}
              </ol>
            </div>
          ))}
        </section>
        <section aria-label="Takes">
          <h3 className="mb-1 font-medium">Takes</h3>
          <Table>
            <thead>
              <tr>
                <Th>Take</Th>
                <Th>Check</Th>
                <Th>Engine</Th>
                <Th>Score</Th>
                <Th>Threshold</Th>
                <Th>Verdict</Th>
              </tr>
            </thead>
            <tbody>
              {takes.flatMap((take) =>
                metricRows(take.checks.verdicts as Json).map((row) => (
                  <tr key={`${take.id}-${row.capability}`}>
                    <Td>
                      {String(take.checks.take_key ?? "")}
                      {take.checks.selected ? " ★" : ""}
                    </Td>
                    <Td>{row.capability}</Td>
                    <Td>
                      <code className="text-xs">{row.adapter}</code>
                    </Td>
                    <Td>{row.score}</Td>
                    <Td>{row.threshold}</Td>
                    <Td title={row.reason}>
                      <Badge
                        variant={
                          row.verdict === "pass"
                            ? "success"
                            : row.verdict === "fail"
                              ? "danger"
                              : row.verdict === "advisory"
                                ? "warning"
                                : "muted"
                        }
                      >
                        {row.verdict}
                      </Badge>
                    </Td>
                  </tr>
                )),
              )}
            </tbody>
          </Table>
          {takes.some((t) => ((t.checks.vlm_judge as Json | undefined)?.defects as unknown[] | undefined)?.length) ? (
            <ul className="mt-2 text-xs">
              {takes.flatMap((t) =>
                (((t.checks.vlm_judge as Json | undefined)?.defects ?? []) as Json[]).map((d, i) => (
                  <li key={`${t.id}-${i}`}>
                    VLM judge, {String(t.checks.take_key)}: {String(d.check)} ({String(d.severity)}) at {String(d.t_s)}{" "}
                    s — {String(d.description)}
                  </li>
                )),
              )}
            </ul>
          ) : null}
        </section>
        <section aria-label="Requested, compiled, observed">
          <h3 className="mb-1 font-medium">Requested → compiled → observed</h3>
          {triad.length === 0 ? <Empty>No coverage report yet.</Empty> : null}
          {triad.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Item</Th>
                  <Th>Requested</Th>
                  <Th>Compiled</Th>
                  <Th>Observed</Th>
                  <Th>Outcome</Th>
                </tr>
              </thead>
              <tbody>
                {triad.map((row) => (
                  <tr key={`${row.item}-${row.dimension}`}>
                    <Td>
                      {row.dimension}
                      <div className="text-xs text-slate-600">{row.item}</div>
                    </Td>
                    <Td>{row.requested}</Td>
                    <Td>{row.compiled}</Td>
                    <Td>{row.observed}</Td>
                    <Td>
                      <Badge variant={badge(row.tone)}>{row.outcome}</Badge>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : null}
        </section>
        {data.world.length ? (
          <section aria-label="World continuity">
            <h3 className="mb-1 font-medium">World continuity</h3>
            <ul className="text-xs">
              {data.world.map((w) => (
                <li key={w.id}>
                  <code>{String(w.checks.node_key)}</code> · environment identity{" "}
                  {w.checks.environment_identity === null || w.checks.environment_identity === undefined
                    ? "not measured"
                    : Number(w.checks.environment_identity).toFixed(2)}{" "}
                  <Badge variant={w.verdict === "warn" ? "warning" : "success"}>{w.verdict}</Badge>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function CritiquePanel({ versionId }: { versionId: string }) {
  const client = useQueryClient();
  const can = useCan("write_content");
  const critiques = useQuery({
    queryKey: keys.critiques(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/critiques", { params: { path: { version_id: versionId } } })),
  });
  const job = useStudioJob([keys.critiques(versionId)]);
  const run = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/versions/{version_id}:critique", { params: { path: { version_id: versionId } } })),
    onSuccess: (data) => job.setJobId(data.job_id),
  });
  const [proposed, setProposed] = useState<Record<string, string>>({});
  const propose = useMutation({
    mutationFn: ({ critiqueId, findingId }: { critiqueId: string; findingId: string }) =>
      unwrap(
        api.POST("/v1/critiques/{critique_id}/findings/{finding_id}:propose", {
          params: { path: { critique_id: critiqueId, finding_id: findingId } },
        }),
      ),
    onSuccess: (data, vars) => {
      setProposed((p) => ({ ...p, [vars.findingId]: data.edit_proposal_id }));
      void client.invalidateQueries({ queryKey: keys.edits(versionId) });
    },
  });
  const latest = critiques.data?.[0];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Creative Director</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div className="flex items-center gap-2">
          <Button onClick={() => run.mutate()} disabled={run.isPending || !can.allowed} title={can.reason}>
            Critique this version
          </Button>
          <JobLine status={job.status} job={job.job} />
        </div>
        <ErrorNote error={run.error ?? propose.error} />
        {!latest ? (
          <Empty>No critique yet. Findings are only proposals: nothing changes until you apply an edit.</Empty>
        ) : null}
        {latest ? (
          <>
            <ul className="grid grid-cols-2 gap-1" aria-label="Critique scores">
              {scoreBars(latest.scores as Json).map((s) => (
                <li key={s.key} title={s.basis} className="flex items-center gap-2">
                  <span className="w-40 capitalize">{s.label}</span>
                  {s.value === null ? (
                    <span className="text-xs text-slate-600">not measured</span>
                  ) : (
                    <span
                      className="inline-block h-2 rounded bg-blue-600"
                      style={{ width: `${Math.round(s.value * 100)}px` }}
                      aria-label={`${s.label} ${s.value.toFixed(2)}`}
                    />
                  )}
                </li>
              ))}
            </ul>
            <ol className="space-y-2" aria-label="Findings">
              {(latest.findings as Json[]).map((f) => {
                const id = String(f.id);
                const ops = (f.proposed_ops ?? []) as unknown[];
                return (
                  <li key={id} className="rounded border border-[var(--color-border)] p-2">
                    <div className="flex items-center gap-2">
                      <Badge variant={f.impact === "high" ? "danger" : f.impact === "medium" ? "warning" : "muted"}>
                        {String(f.impact)}
                      </Badge>
                      <span>{String(f.issue)}</span>
                    </div>
                    <div className="mt-1 flex items-center gap-2 text-xs text-slate-600">
                      {humanize(String(f.category))}
                      {ops.length ? (
                        proposed[id] ? (
                          <span>Edit proposed — review it in the edit panel.</span>
                        ) : (
                          <Button
                            variant="outline"
                            onClick={() => propose.mutate({ critiqueId: latest.id, findingId: id })}
                            disabled={propose.isPending || !can.allowed}
                            title={can.reason}
                          >
                            Propose edit
                          </Button>
                        )
                      ) : (
                        <span>{String((f.estimate as Json | undefined)?.note ?? "")}</span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function VersionConsistency({ versionId }: { versionId: string }) {
  const reports = useQuery({
    queryKey: keys.versionConsistency(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/consistency", { params: { path: { version_id: versionId } } })),
  });
  if (!reports.data?.length) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Creator consistency</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {reports.data.map((r) => {
          const dims = ((r.metrics as Json).dimensions ?? {}) as Record<string, Json>;
          return (
            <div key={r.id}>
              <Badge variant={r.verdict === "in_band" ? "success" : "warning"}>{humanize(r.verdict)}</Badge>{" "}
              <Link className="underline" href={`/creators/${r.creator_id}?tab=consistency`}>
                creator history
              </Link>
              <ul className="mt-1 flex flex-wrap gap-1">
                {Object.entries(dims).map(([k, v]) => (
                  <li key={k}>
                    <Badge variant={badge(statusTone(String(v.status)))} title={String(v.reason ?? v.method ?? "")}>
                      {humanize(k)}: {humanize(String(v.status))}
                      {v.mock ? " (mock)" : ""}
                    </Badge>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
