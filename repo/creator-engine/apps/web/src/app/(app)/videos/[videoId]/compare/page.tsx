"use client";
/**
 * Compare two versions (§31 Versions panel, §12.8): synchronized side-by-side players, and the
 * spec, intent, CBS and coverage differences from `GET /v1/videos/{id}/compare`.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use, useRef } from "react";

import { PageHeader } from "@/components/app-shell";
import { Player } from "@/components/player";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { groupDiff, readablePath, short } from "@/lib/edits";
import { humanize } from "@/lib/format";
import { useCompare, useVersion } from "@/lib/queries";

type Json = Record<string, unknown>;

function DiffTable({ entries, label }: { entries: unknown[]; label: string }) {
  const groups = groupDiff(entries);
  if (!groups.length) return <Empty>No differences.</Empty>;
  return (
    <Table aria-label={label}>
      <thead>
        <tr>
          <Th>Area</Th>
          <Th>Where</Th>
          <Th>Before</Th>
          <Th>After</Th>
        </tr>
      </thead>
      <tbody>
        {groups.flatMap(([area, rows]) =>
          rows.map((r) => (
            <tr key={`${area}${r.path}${r.kind}`} data-testid="diff-row">
              <Td>{humanize(area)}</Td>
              <Td className="font-mono text-xs">{readablePath(r.path)}</Td>
              <Td className="text-xs">{r.kind === "added" ? "—" : short(r.before)}</Td>
              <Td className="text-xs">{r.kind === "removed" ? "—" : short(r.after)}</Td>
            </tr>
          )),
        )}
      </tbody>
    </Table>
  );
}

/** Coverage summary counts side by side ("level:HONORED", "outcome:…", "delivered"). */
function SummaryTable({ a, b }: { a: Json | null; b: Json | null }) {
  const keys = [...new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})])].sort();
  if (!keys.length) return <p className="text-slate-600">No coverage yet.</p>;
  const label = (key: string) => {
    const [kind, value] = key.includes(":") ? key.split(":", 2) : ["", key];
    return kind ? `${humanize(kind)}: ${humanize((value ?? "").toLowerCase())}` : humanize(value);
  };
  return (
    <Table aria-label="Coverage summary">
      <thead>
        <tr>
          <Th>Count</Th>
          <Th>A</Th>
          <Th>B</Th>
        </tr>
      </thead>
      <tbody>
        {keys.map((key) => {
          const left = a?.[key] ?? "—";
          const right = b?.[key] ?? "—";
          return (
            <tr key={key}>
              <Td className="text-xs">{label(key)}</Td>
              <Td className="text-xs tabular-nums">{String(left)}</Td>
              <Td className={left === right ? "text-xs tabular-nums" : "text-xs font-semibold tabular-nums"}>
                {String(right)}
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function Compare({ videoId }: { videoId: string }) {
  const params = useSearchParams();
  const a = params.get("a");
  const b = params.get("b");
  const compare = useCompare(videoId, a, b);
  const va = useVersion(a);
  const vb = useVersion(b);
  const left = useRef<HTMLVideoElement>(null);
  const right = useRef<HTMLVideoElement>(null);
  if (!a || !b) return <Alert tone="warning">Pick two versions to compare (from the Versions panel).</Alert>;
  if (compare.isLoading) return <Skeleton className="h-64" />;
  if (compare.error || !compare.data) return <Alert tone="danger">{compare.error?.message ?? "Not found."}</Alert>;
  const data = compare.data;
  const coverage = data.coverage as Json;
  const changed = (coverage.changed ?? []) as Json[];
  const playBoth = async () => {
    for (const video of [left.current, right.current]) {
      if (!video) continue;
      video.currentTime = 0;
      await video.play().catch(() => undefined);
    }
  };
  return (
    <>
      <PageHeader
        title="Compare versions"
        description={`v${va.data?.number ?? "?"} → v${vb.data?.number ?? "?"}`}
        actions={
          <Button variant="outline" asChild>
            <Link href={`/videos/${videoId}?version=${b}`}>Back to the studio</Link>
          </Button>
        }
      />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {[
          { label: "A", version: va.data, ref: left, id: a },
          { label: "B", version: vb.data, ref: right, id: b },
        ].map(({ label, version, ref, id }) => (
          <Card key={label}>
            <CardHeader>
              <CardTitle>
                {label}: v{version?.number ?? "?"} · {humanize(version?.origin)}{" "}
                {version ? <StateBadge state={version.state} /> : null}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <Player
                versionId={id}
                state={version?.state ?? ""}
                videoRef={ref}
                testId={`player-${label.toLowerCase()}`}
              />
            </CardContent>
          </Card>
        ))}
      </div>
      <div className="mt-2">
        <Button variant="outline" onClick={playBoth}>
          Play both from the start
        </Button>
      </div>
      <div className="mt-4 flex flex-col gap-4">
        <Card>
          <CardHeader>
            <CardTitle>Spec</CardTitle>
          </CardHeader>
          <CardContent>
            <DiffTable entries={data.spec as unknown[]} label="Spec differences" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Intent</CardTitle>
          </CardHeader>
          <CardContent>
            <DiffTable entries={data.intent as unknown[]} label="Intent differences" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Canonical behavior (CBS)</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2">
            {!data.cbs_available.a || !data.cbs_available.b ? (
              <Alert tone="info">Behavior is compared once both versions have been previzualized or built.</Alert>
            ) : Object.keys(data.cbs).length ? (
              Object.entries(data.cbs).map(([scene, entries]) => (
                <div key={scene}>
                  <p className="font-mono text-xs font-semibold">{scene}</p>
                  <DiffTable entries={entries as unknown[]} label={`CBS differences in ${scene}`} />
                </div>
              ))
            ) : (
              <Empty>The resolved behavior is identical.</Empty>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Coverage</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            <SummaryTable a={coverage.a as Json | null} b={coverage.b as Json | null} />
            {changed.length ? (
              <Table aria-label="Coverage differences">
                <thead>
                  <tr>
                    <Th>Item</Th>
                    <Th>A</Th>
                    <Th>B</Th>
                  </tr>
                </thead>
                <tbody>
                  {changed.map((c) => {
                    const side = (v: unknown) => {
                      const e = (v ?? null) as Json | null;
                      const compiled = (e?.compiled ?? null) as Json | null;
                      if (!compiled) return "—";
                      const outcome = e?.outcome ? ` · ${humanize(String(e.outcome).toLowerCase())}` : "";
                      return `${String(compiled.level)} · ${humanize(String(compiled.method))}${outcome}`;
                    };
                    return (
                      <tr key={`${String(c.item_ref)}|${String(c.dimension)}`}>
                        <Td className="text-xs">
                          <span className="font-mono">{readablePath(String(c.item_ref))}</span> ·{" "}
                          {humanize(String(c.dimension))}
                        </Td>
                        <Td className="text-xs">{side(c.a)}</Td>
                        <Td className="text-xs">{side(c.b)}</Td>
                      </tr>
                    );
                  })}
                </tbody>
              </Table>
            ) : (
              <p className="text-slate-600">
                <Badge variant="muted">no coverage change</Badge>
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}

export default function ComparePage({ params }: { params: Promise<{ videoId: string }> }) {
  const { videoId } = use(params);
  return (
    <Suspense fallback={<Skeleton className="h-64" />}>
      <Compare videoId={videoId} />
    </Suspense>
  );
}
