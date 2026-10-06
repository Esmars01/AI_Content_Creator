"use client";
/** Creator Studio → Consistency (§20, Phase 11): every video's consistency report against the
 * creator's baselines, as a status grid across videos, plus the rolling baselines. Automated metrics
 * do not judge personality or "same person": the rating queue collects those human judgements. */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { consistencySeries, type Json, statusTone } from "@/lib/qc";

const SYMBOL: Record<string, string> = { in_band: "●", out_of_band: "▲", deviation: "◆", not_measured: "–" };

export function ConsistencyPanel({ creatorId }: { creatorId: string }) {
  const reports = useQuery({
    queryKey: ["creator-consistency", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/consistency", { params: { path: { creator_id: creatorId } } })),
  });
  const baselines = useQuery({
    queryKey: ["baselines", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/baselines", { params: { path: { creator_id: creatorId } } })),
  });
  if (reports.isLoading) return <Skeleton className="h-40" />;
  if (reports.error) return <ErrorNote error={reports.error} />;
  const { dimensions, points } = consistencySeries(reports.data as unknown as Json[]);
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Consistency across videos</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {points.length === 0 ? (
            <Empty>No consistency report yet: one is written after each built video of this creator.</Empty>
          ) : (
            <Table aria-label="Consistency grid">
              <thead>
                <tr>
                  <Th>Dimension</Th>
                  {points.map((p) => (
                    <Th key={p.versionId} title={p.versionId}>
                      {when(p.createdAt)}
                    </Th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {dimensions.map((d) => (
                  <tr key={d}>
                    <Td>{humanize(d)}</Td>
                    {points.map((p) => {
                      const status = p.status[d] ?? "not_measured";
                      const tone = statusTone(status);
                      return (
                        <Td key={p.versionId} title={humanize(status)}>
                          <Badge variant={tone === "neutral" ? "muted" : tone}>{SYMBOL[status] ?? "?"}</Badge>
                        </Td>
                      );
                    })}
                  </tr>
                ))}
                <tr>
                  <Td>Verdict</Td>
                  {points.map((p) => (
                    <Td key={p.versionId}>{humanize(p.verdict)}</Td>
                  ))}
                </tr>
              </tbody>
            </Table>
          )}
          <p className="mt-2 text-xs text-slate-600">
            ● in band · ▲ out of band (warns unless promoted to a gate) · ◆ intentional deviation (spec override) · –
            not measured. Human judgements:{" "}
            <Link className="underline" href="/ratings">
              rating queue
            </Link>
            .
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Baselines</CardTitle>
        </CardHeader>
        <CardContent className="text-sm">
          {!baselines.data?.length ? <Empty>No baseline yet: run a Creator Test.</Empty> : null}
          <ul>
            {(baselines.data ?? []).map((b) => (
              <li key={b.id}>
                {humanize(b.source)} · {b.window_n} video{b.window_n === 1 ? "" : "s"} · {when(b.created_at)}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
