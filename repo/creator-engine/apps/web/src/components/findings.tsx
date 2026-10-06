"use client";
import { Badge } from "@/components/ui/badge";
import { Empty } from "@/components/ui/misc";
import type { PlanReport } from "@/lib/api";
import { humanize } from "@/lib/format";

type Finding = NonNullable<PlanReport["findings"]>[number];

const SEVERITY = { blocking: "danger", warning: "warning", info: "muted" } as const;

export function FindingsList({ findings, empty }: { findings: readonly Finding[]; empty: string }) {
  if (!findings.length) return <Empty>{empty}</Empty>;
  return (
    <ul className="flex flex-col gap-2">
      {findings.map((finding, index) => (
        <li key={`${finding.kind}:${index}`} className="flex items-start gap-2 text-sm">
          <Badge variant={SEVERITY[finding.severity ?? "warning"]}>{finding.severity ?? "warning"}</Badge>
          <div>
            <span>{finding.message}</span>
            <span className="ml-2 text-xs text-slate-600">{humanize(finding.kind)}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}
