"use client";
import { Table, Td, Th } from "@/components/ui/misc";
import {
  badgeFor,
  type CoverageEntry,
  type CoverageSummary,
  summarize,
  summaryText,
  TONE_CLASSES,
} from "@/lib/coverage";
import { humanize } from "@/lib/format";
import { cn } from "@/lib/utils";

/** One item's badge: delivered only for *_CONFIRMED outcomes (I9). */
export function CoverageBadge({ entry }: { entry: Pick<CoverageEntry, "outcome" | "compiled"> }) {
  const badge = badgeFor(entry);
  return (
    <span
      title={badge.explanation}
      data-delivered={badge.delivered ? "true" : "false"}
      className={cn("inline-flex rounded-full border px-2 py-0.5 text-xs font-medium", TONE_CLASSES[badge.tone])}
    >
      {badge.label}
    </span>
  );
}

/** The Simple view's per-scene badge ("4 honored, 6 approximated, 2 unsupported"). */
export function SceneCoverageBadge({ entries }: { entries: readonly CoverageEntry[] }) {
  const summary: CoverageSummary = summarize(entries);
  if (summary.total === 0) return <span className="text-xs text-slate-600">no behavior items</span>;
  return (
    <span
      data-testid="scene-coverage"
      data-delivered={summary.delivered}
      className="inline-flex rounded-full border border-slate-300 bg-slate-50 px-2 py-0.5 text-xs text-slate-800"
      title={
        summary.observed === 0
          ? "Predicted from the routed engines; nothing is delivered until it is generated and observed."
          : "Delivered counts only items the analyzers confirmed."
      }
    >
      {summaryText(summary)}
    </span>
  );
}

/** Advanced view: requested → compiled → observed per item. */
export function CoverageTable({ entries }: { entries: readonly CoverageEntry[] }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Item</Th>
          <Th>Requested</Th>
          <Th>Compiled</Th>
          <Th>Observed</Th>
          <Th>Status</Th>
        </tr>
      </thead>
      <tbody>
        {entries.map((entry) => (
          <tr key={`${entry.item_ref}:${entry.dimension}`}>
            <Td className="font-mono text-xs">
              {entry.item_ref}
              <div className="text-slate-600">{humanize(entry.dimension)}</div>
            </Td>
            <Td>{entry.requested}</Td>
            <Td>
              {humanize(entry.compiled.level.toLowerCase())} · {humanize(entry.compiled.method)}
              {entry.compiled.detail ? <div className="text-xs text-slate-600">{entry.compiled.detail}</div> : null}
            </Td>
            <Td>
              {entry.observed
                ? `${humanize(entry.observed.verdict.toLowerCase())} (${Math.round(entry.observed.confidence * 100)} %)`
                : "—"}
            </Td>
            <Td>
              <CoverageBadge entry={entry} />
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}
