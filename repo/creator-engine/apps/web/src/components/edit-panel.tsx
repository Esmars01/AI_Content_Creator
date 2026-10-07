"use client";
/**
 * The NL edit panel and proposal cards (§31, §28): an instruction (with an optional selection)
 * becomes a proposal — operations, diff, impact (regenerate / keep / cascade / no visible effect),
 * the predicted coverage delta, alternatives and cost — that the user applies or rejects. Nothing
 * changes until Apply; applying creates a new version (versions are never mutated).
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Textarea } from "@/components/ui/input";
import { Alert, Empty, Table, Td, Th } from "@/components/ui/misc";
import { api, ApiError, idempotencyKey, unwrap } from "@/lib/api";
import {
  coverageChanges,
  describeOp,
  type EditProposal,
  groupDiff,
  type Operation,
  readablePath,
  short,
  summarizeImpact,
  timeRange,
} from "@/lib/edits";
import { humanize, usd, when } from "@/lib/format";
import { keys, useEdit, useEdits, useVersion } from "@/lib/queries";
import { useCan } from "@/lib/roles";
import { useActiveProposal, useStudio } from "@/lib/store";

const STATUS_TONE: Record<string, "info" | "success" | "danger" | "muted" | "warning"> = {
  proposing: "info",
  proposed: "warning",
  applied: "success",
  failed: "danger",
  rejected: "muted",
  superseded: "muted",
};

const ALTERNATIVE_LABELS: Record<string, string> = {
  full_reperformance: "Full re-performance",
  editorial_only: "Editorial only (punch-ins, no new performance)",
  lipsync_patch: "Lip-sync patch (keep the performance, re-sync the mouth)",
};

function ErrorAlert({ error }: { error: Error }) {
  return (
    <Alert tone="danger">
      {error.message}
      {error instanceof ApiError ? error.issues.map((i, n) => <div key={n}>{i.message}</div>) : null}
    </Alert>
  );
}

function ImpactBlock({ proposal }: { proposal: EditProposal }) {
  const impact = summarizeImpact(proposal.impact);
  return (
    <section aria-label="Impact" className="flex flex-col gap-1 text-sm">
      <h3 className="font-medium">Impact</h3>
      <p data-testid="impact-summary">
        <Badge variant="warning">{impact.regenerate.length} regenerate</Badge>{" "}
        <Badge variant="info">{impact.cascade.length} cascade</Badge>{" "}
        <Badge variant="success">{impact.keep} keep</Badge>{" "}
        {impact.noVisibleEffect.length ? (
          <Badge variant="muted">{impact.noVisibleEffect.length} no visible effect</Badge>
        ) : null}{" "}
        · cost {usd(impact.estimateUsd)}
        {impact.gpuSeconds ? ` · ${Math.round(impact.gpuSeconds)} GPU s` : ""}
      </p>
      {impact.generation.length ? (
        <p className="text-xs text-slate-700">
          Re-generated: <span className="font-mono">{impact.generation.join(", ")}</span>
        </p>
      ) : (
        <p className="text-xs text-slate-700">No model re-generates anything: only cheap steps re-run.</p>
      )}
      {impact.noVisibleEffect.length ? (
        <ul className="text-xs text-slate-700" aria-label="No visible effect">
          {impact.noVisibleEffect.map((n) => (
            <li key={n.key}>
              <span className="font-mono">{n.key}</span>: {n.reason}
            </li>
          ))}
        </ul>
      ) : null}
      {impact.locksBlocking.length ? (
        <ul className="text-xs text-amber-900" aria-label="Held by locks">
          {impact.locksBlocking.map((l, n) => (
            <li key={n}>
              🔒 {humanize(l.label)}: {l.reason}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function DiffBlock({ proposal }: { proposal: EditProposal }) {
  const groups = groupDiff((((proposal.impact ?? {}) as Record<string, unknown>).diff as unknown[]) ?? []);
  if (!groups.length) return null;
  return (
    <section aria-label="Changes" className="flex flex-col gap-1 text-sm">
      <h3 className="font-medium">Changes</h3>
      {groups.map(([area, entries]) => (
        <div key={area}>
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-600">{humanize(area)}</p>
          <ul className="text-xs">
            {entries.slice(0, 12).map((e) => (
              <li key={e.path + e.kind}>
                <span className="font-mono">{readablePath(e.path)}</span>:{" "}
                {e.kind === "added"
                  ? `added ${short(e.after)}`
                  : e.kind === "removed"
                    ? `removed ${short(e.before)}`
                    : `${short(e.before)} → ${short(e.after)}`}
              </li>
            ))}
            {entries.length > 12 ? <li>… {entries.length - 12} more</li> : null}
          </ul>
        </div>
      ))}
    </section>
  );
}

function CoverageBlock({ proposal }: { proposal: EditProposal }) {
  const rows = coverageChanges(proposal.coverage_delta);
  if (!rows.length) return null;
  return (
    <section aria-label="Coverage delta" className="flex flex-col gap-1 text-sm">
      <h3 className="font-medium">Predicted coverage</h3>
      <Table>
        <thead>
          <tr>
            <Th>Item</Th>
            <Th>Before</Th>
            <Th>After</Th>
            <Th>Note</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={`${r.itemRef}|${r.dimension}`}>
              <Td className="text-xs">
                <span className="font-mono">{readablePath(r.itemRef)}</span> · {humanize(r.dimension)}
              </Td>
              <Td className="text-xs">{r.before ?? "—"}</Td>
              <Td className="text-xs">{r.after ?? "—"}</Td>
              <Td className="text-xs">
                {r.blockedByLock ? <Badge variant="warning">🔒 {r.blockedByLock}</Badge> : null}
                {r.noVisibleEffect ? <Badge variant="muted">no visible effect</Badge> : null}{" "}
                {r.reason ?? humanize(r.change)}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </section>
  );
}

/**
 * One proposal: what it does, what it costs, and Apply / Reject. Applying opens the new version of
 * the proposal's own video (from the version it was proposed on), never the page it is shown on.
 */
export function ProposalCard({ proposalId, onClose }: { proposalId: string; onClose?: () => void }) {
  const router = useRouter();
  const client = useQueryClient();
  const edit = useEdit(proposalId);
  const source = useVersion(edit.data?.version_id ?? null);
  const videoId = source.data?.video_id ?? null;
  const can = useCan("write_content");
  const [alternative, setAlternative] = useState("full_reperformance");
  const apply = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/edits/{edit_proposal_id}:apply", {
          params: { path: { edit_proposal_id: proposalId } },
          headers: idempotencyKey(),
          body: {
            alternative:
              alternative === "full_reperformance" ? null : (alternative as "editorial_only" | "lipsync_patch"),
          },
        }),
      ),
    onSuccess: (accepted) => {
      if (videoId) {
        router.replace(`/videos/${videoId}?version=${accepted.new_version_id}`);
        void client.invalidateQueries({ queryKey: keys.versions(videoId) });
      }
      void client.invalidateQueries({ queryKey: keys.edit(proposalId) });
    },
  });
  const reject = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/edits/{edit_proposal_id}:reject", { params: { path: { edit_proposal_id: proposalId } } })),
    onSuccess: async (row) => {
      client.setQueryData(keys.edit(proposalId), row);
      await client.invalidateQueries({ queryKey: keys.edits(row.version_id) });
    },
  });
  const p = edit.data;
  if (!p) return <Empty>Loading the proposal…</Empty>;
  const impact = summarizeImpact(p.impact);
  const ops = (p.ops ?? []) as Operation[];
  const kind = String((p.selection as Record<string, unknown>).kind ?? "edit");
  const alternatives = (p.alternatives ?? []) as { strategy: string; estimate?: { usd?: number } }[];
  const strategies = [
    "full_reperformance",
    ...alternatives.map((a) => a.strategy).filter((s) => s !== "full_reperformance"),
  ];
  return (
    <Card data-testid="proposal-card" aria-label="Edit proposal">
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle>{p.instruction ? `“${p.instruction}”` : humanize(kind)}</CardTitle>
          <Badge variant={STATUS_TONE[p.status] ?? "muted"} data-testid="proposal-status">
            {p.status}
          </Badge>
        </div>
        <CardDescription>
          {impact.planner ? `Planned by: ${impact.planner}` : null} · {when(p.created_at)}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {p.status === "proposing" ? (
          <p className="text-sm" aria-live="polite">
            Working out the edit…
          </p>
        ) : null}
        {impact.issues.length ? (
          <Alert tone="danger" aria-label="Issues">
            {impact.issues.map((i, n) => (
              <div key={n}>
                {i.message}
                {i.path ? <span className="font-mono text-xs"> ({i.path})</span> : null}
              </div>
            ))}
          </Alert>
        ) : null}
        {ops.length ? (
          <section aria-label="Operations" className="text-sm">
            <h3 className="font-medium">Operations</h3>
            <ol className="list-decimal pl-5" data-testid="proposal-ops">
              {ops.map((op, n) => (
                <li key={n}>
                  {describeOp(op)}
                  {op.reason ? <span className="text-xs text-slate-600"> — {op.reason}</span> : null}
                </li>
              ))}
            </ol>
          </section>
        ) : null}
        {impact.assumptions.length || impact.notes.length ? (
          <ul className="text-xs text-slate-700" aria-label="Assumptions">
            {[...impact.assumptions, ...impact.notes].map((a, n) => (
              <li key={n}>{a}</li>
            ))}
          </ul>
        ) : null}
        {p.status !== "proposing" && p.status !== "failed" ? (
          <>
            <DiffBlock proposal={p} />
            <ImpactBlock proposal={p} />
            <CoverageBlock proposal={p} />
          </>
        ) : null}
        {p.status === "proposed" && strategies.length > 1 ? (
          <fieldset className="flex flex-col gap-1 text-sm">
            <legend className="font-medium">Strategy</legend>
            {strategies.map((strategy) => {
              const alt = alternatives.find((a) => a.strategy === strategy);
              return (
                <label key={strategy} className="flex items-center gap-2">
                  <input
                    type="radio"
                    name={`alternative-${proposalId}`}
                    value={strategy}
                    checked={alternative === strategy}
                    onChange={() => setAlternative(strategy)}
                  />
                  {ALTERNATIVE_LABELS[strategy] ?? humanize(strategy)}
                  {alt?.estimate?.usd !== undefined ? (
                    <span className="text-xs text-slate-600">({usd(alt.estimate.usd)})</span>
                  ) : null}
                </label>
              );
            })}
          </fieldset>
        ) : null}
        {apply.error ? <ErrorAlert error={apply.error} /> : null}
        {reject.error ? <ErrorAlert error={reject.error} /> : null}
        {p.status === "applied" && p.result_version_id && videoId ? (
          <Alert tone="success">
            Applied as a new version.{" "}
            <button
              type="button"
              className="underline"
              onClick={() => router.replace(`/videos/${videoId}?version=${p.result_version_id}`)}
            >
              Open it
            </button>
          </Alert>
        ) : null}
        <div className="flex gap-2">
          {p.status === "proposed" ? (
            <Button
              onClick={() => apply.mutate()}
              disabled={apply.isPending || !videoId || !can.allowed}
              title={can.reason}
              data-testid="apply-edit"
            >
              {apply.isPending ? "Applying…" : "Apply"}
            </Button>
          ) : null}
          {/* a failed proposal changed nothing: there is nothing to reject, only to close */}
          {p.status === "proposed" ? (
            <Button
              variant="outline"
              onClick={() => reject.mutate()}
              disabled={reject.isPending || !can.allowed}
              title={can.reason}
            >
              Reject
            </Button>
          ) : null}
          {onClose ? (
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

/** "make him more skeptical": the instruction box, the selection, the proposal and the history. */
export function EditPanel({ versionId, sceneKeys }: { versionId: string; sceneKeys: string[] }) {
  const client = useQueryClient();
  const showProposal = useStudio((s) => s.showProposal);
  const activeProposal = useActiveProposal(versionId);
  const history = useEdits(versionId);
  const [instruction, setInstruction] = useState("");
  const [scenes, setScenes] = useState<string[]>([]);
  const [range, setRange] = useState<{ start: string; end: string }>({ start: "", end: "" });
  const span = timeRange(range);
  const can = useCan("write_content");
  const propose = useMutation({
    mutationFn: () => {
      const selection =
        scenes.length || span.value
          ? {
              ...(scenes.length ? { scene_keys: scenes } : {}),
              ...(span.value ? { time_range_s: span.value } : {}),
            }
          : null;
      return unwrap(
        api.POST("/v1/versions/{version_id}/edits", {
          params: { path: { version_id: versionId } },
          headers: idempotencyKey(),
          body: { instruction: instruction.trim(), selection },
        }),
      );
    },
    onSuccess: async (accepted) => {
      showProposal(versionId, accepted.edit_proposal_id);
      setInstruction("");
      await client.invalidateQueries({ queryKey: keys.edits(versionId) });
    },
  });
  const past = (history.data ?? []).filter((e) => e.id !== activeProposal);
  return (
    <Card id="edit">
      <CardHeader>
        <CardTitle>Edit</CardTitle>
        <CardDescription>
          Describe the change. You will see what it does and what it costs before anything changes.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (instruction.trim() && !span.error && can.allowed) propose.mutate();
          }}
        >
          <Label htmlFor="edit-instruction">Instruction</Label>
          <Textarea
            id="edit-instruction"
            rows={2}
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="make him more skeptical"
          />
          {sceneKeys.length ? (
            <fieldset className="flex flex-wrap items-center gap-3 text-sm">
              <legend className="sr-only">Selection</legend>
              <span className="text-slate-700">Only in:</span>
              {sceneKeys.map((key) => (
                <label key={key} className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    aria-label={`Scene ${key}`}
                    checked={scenes.includes(key)}
                    onChange={(e) => setScenes((s) => (e.target.checked ? [...s, key] : s.filter((k) => k !== key)))}
                  />
                  <span className="font-mono text-xs">{key}</span>
                </label>
              ))}
            </fieldset>
          ) : null}
          <div className="flex items-center gap-2 text-sm">
            <span className="text-slate-700">Time range (s):</span>
            <Input
              aria-label="Range start (seconds)"
              className="w-20"
              inputMode="decimal"
              value={range.start}
              onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))}
            />
            –
            <Input
              aria-label="Range end (seconds)"
              className="w-20"
              inputMode="decimal"
              value={range.end}
              onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))}
            />
          </div>
          {span.error ? (
            <p className="text-xs text-red-800" data-testid="range-error">
              {span.error}
            </p>
          ) : null}
          {propose.error ? <ErrorAlert error={propose.error} /> : null}
          <div>
            <Button
              type="submit"
              disabled={!instruction.trim() || Boolean(span.error) || propose.isPending || !can.allowed}
              title={can.reason}
              data-testid="propose-edit"
            >
              {propose.isPending ? "Sending…" : "Propose"}
            </Button>
          </div>
        </form>
        {activeProposal ? (
          <ProposalCard proposalId={activeProposal} onClose={() => showProposal(versionId, null)} />
        ) : null}
        {past.length ? (
          <section aria-label="Earlier proposals" className="text-sm">
            <h3 className="font-medium">Earlier proposals</h3>
            <ul className="flex flex-col gap-1">
              {past.slice(0, 10).map((e) => (
                <li key={e.id} className="flex items-center justify-between gap-2">
                  <button
                    type="button"
                    className="truncate text-left text-blue-800 hover:underline"
                    onClick={() => showProposal(versionId, e.id)}
                  >
                    {e.instruction || humanize(String((e.selection as Record<string, unknown>).kind ?? "edit"))}
                  </button>
                  <Badge variant={STATUS_TONE[e.status] ?? "muted"}>{e.status}</Badge>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** Sends structured operations (the Advanced editors) as an edit and shows its proposal. */
export function useStructuredEdit(versionId: string) {
  const client = useQueryClient();
  const showProposal = useStudio((s) => s.showProposal);
  return useMutation({
    mutationFn: (operations: Operation[]) =>
      unwrap(
        api.POST("/v1/versions/{version_id}/edits", {
          params: { path: { version_id: versionId } },
          headers: idempotencyKey(),
          body: { instruction: "", operations },
        }),
      ),
    onSuccess: async (accepted) => {
      showProposal(versionId, accepted.edit_proposal_id);
      await client.invalidateQueries({ queryKey: keys.edits(versionId) });
    },
  });
}
