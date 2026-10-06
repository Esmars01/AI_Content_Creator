"use client";
/** Building blocks shared by the previz review and the Video Studio (display only). */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useAdvanced } from "@/components/app-shell";
import { CoverageTable, SceneCoverageBadge } from "@/components/coverage";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton } from "@/components/ui/misc";
import { api, ApiError, type Domain, idempotencyKey, type Schemas, unwrap, type VideoSpec } from "@/lib/api";
import { bySceneKey, type CoverageEntry } from "@/lib/coverage";
import { humanize } from "@/lib/format";
import { keys, useStoryboard } from "@/lib/queries";

type Scene = Domain["Scene"];

export function scenesOf(spec: VideoSpec): Scene[] {
  return [...(spec.scenes ?? [])].sort((a, b) => a.order - b.order);
}

export function segmentScenes(spec: VideoSpec): Record<string, string> {
  const out: Record<string, string> = {};
  for (const scene of spec.scenes ?? []) for (const key of scene.segment_keys ?? []) out[key] = scene.key;
  return out;
}

/** The script with annotation chips (pauses, emphasis, non-verbal audio, …). */
export function ScriptView({ spec }: { spec: VideoSpec }) {
  const segments = spec.script.segments ?? [];
  const locked = (spec as VideoSpec & { script: { wording_locked?: boolean } }).script.wording_locked;
  return (
    <div className="flex flex-col gap-2" data-testid="script">
      {locked ? <Badge variant="info">wording locked</Badge> : null}
      {segments.map((segment) => (
        <p key={segment.key} className="text-sm leading-relaxed">
          <span className="mr-2 font-mono text-xs text-slate-500">{segment.key}</span>
          {segment.text}
          {(segment.annotations ?? []).map((annotation) => (
            <span
              key={annotation.key}
              className="ml-1 inline-flex rounded border border-slate-300 bg-slate-50 px-1 text-[11px] text-slate-700"
              title={`${humanize(annotation.type)} at word ${annotation.span.start.word}`}
            >
              {annotation.tag || humanize(annotation.type)}
            </span>
          ))}
        </p>
      ))}
    </div>
  );
}

export function Storyboard({ versionId }: { versionId: string }) {
  const storyboard = useStoryboard(versionId);
  if (storyboard.isLoading) return <Skeleton className="h-32" />;
  const shots = storyboard.data?.shots ?? [];
  if (!shots.length) return <Empty>No shots.</Empty>;
  return (
    <ol className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4" aria-label="Storyboard">
      {shots.map((shot) => {
        const image = shot.keyframe ?? shot.plate;
        return (
          <li
            key={shot.shot_key}
            className="flex flex-col gap-1 rounded-md border border-slate-200 p-2"
            data-testid="storyboard-shot"
          >
            <div className="flex aspect-[9/16] max-h-48 items-center justify-center overflow-hidden rounded bg-slate-100">
              {image ? (
                <img
                  src={image.url}
                  alt={`${shot.keyframe ? "Keyframe" : "World plate"} for ${shot.shot_key}`}
                  className="h-full object-cover"
                />
              ) : (
                <span className="p-2 text-center text-xs text-slate-600">{humanize(shot.type)}</span>
              )}
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium">
                {shot.order + 1}. {humanize(shot.type)}
              </span>
              <span className="text-slate-600">{shot.keyframe ? "keyframe" : shot.plate ? "plate" : ""}</span>
            </div>
            <p className="line-clamp-3 text-xs text-slate-700">{shot.text || "—"}</p>
          </li>
        );
      })}
    </ol>
  );
}

/** Intent: plain sentences (Simple) or fields with their derived_from trace (Advanced). */
export function IntentPanel({ intent }: { intent: Schemas["IntentOut"] | undefined }) {
  const advanced = useAdvanced();
  if (!intent) return <Skeleton className="h-24" />;
  const video = intent.video as Record<string, unknown>;
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm">
        <span className="font-medium">Video:</span> {humanize(String(video.narrative_goal ?? "—"))}
        {video.audience_effect ? `; effect: ${humanize(String(video.audience_effect))}` : ""}
        {Array.isArray(video.emotional_arc) && video.emotional_arc.length
          ? `; arc: ${(video.emotional_arc as string[]).join(" → ")}`
          : ""}
      </p>
      <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {intent.scenes.map((scene) => {
          const fields = scene.intent as Record<string, unknown>;
          return (
            <li key={scene.scene_key} className="rounded-md border border-slate-200 p-2 text-sm">
              <div className="font-medium">{scene.scene_key}</div>
              <div>
                Goal: {humanize(String(fields.narrative_goal ?? "—"))}
                {fields.audience_effect ? `; effect: ${humanize(String(fields.audience_effect))}` : ""}
              </div>
              {advanced ? (
                <dl className="mt-1 grid grid-cols-2 gap-x-2 text-xs text-slate-700">
                  {Object.entries(fields)
                    .filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
                    .map(([k, v]) => (
                      <div key={k} className="contents">
                        <dt className="text-slate-500">{humanize(k)}</dt>
                        <dd>{Array.isArray(v) ? v.join(", ") : String(v)}</dd>
                      </div>
                    ))}
                </dl>
              ) : null}
              {advanced && scene.decisions.length ? (
                <ul className="mt-1 text-xs text-slate-700">
                  {scene.decisions.map((d, i) => (
                    <li key={i}>
                      {d.applied ? "✓" : "–"} {String(d.rule_id)}: {String(d.effect ?? "")}
                    </li>
                  ))}
                </ul>
              ) : null}
              {advanced && scene.derived.length ? (
                <p className="mt-1 text-xs text-slate-500">
                  {scene.derived.length} elements derived from intent or the compiler
                </p>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Coverage per scene (Simple badges) and the full table (Advanced). */
export function CoveragePanel({ spec, entries }: { spec: VideoSpec; entries: readonly CoverageEntry[] }) {
  const advanced = useAdvanced();
  const grouped = bySceneKey(entries, segmentScenes(spec));
  return (
    <div className="flex flex-col gap-3">
      <ul className="flex flex-col gap-1 text-sm">
        {scenesOf(spec).map((scene) => (
          <li key={scene.key} className="flex items-center gap-2">
            <span className="w-24 font-medium">{scene.key}</span>
            <SceneCoverageBadge entries={grouped[scene.key] ?? []} />
          </li>
        ))}
      </ul>
      {advanced ? <CoverageTable entries={entries} /> : null}
    </div>
  );
}

/** Approve, with overrides for overridable blocking findings (each needs a reason, audit-logged). */
export function ApprovePanel({
  versionId,
  videoId,
  blocking,
  disabled,
  state,
}: {
  versionId: string;
  videoId: string;
  blocking: Schemas["BlockingFinding"][];
  disabled: boolean;
  state: string;
}) {
  const router = useRouter();
  const client = useQueryClient();
  const [overrides, setOverrides] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/versions/{version_id}:approve", {
          params: { path: { version_id: versionId } },
          headers: idempotencyKey(),
          body: { overrides, reason: reason || null },
        }),
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.version(versionId) });
      router.push(`/videos/${videoId}?version=${versionId}`);
    },
  });
  const nonOverridable = blocking.filter((f) => !f.overridable);
  const needsReason = overrides.length > 0 && !reason.trim();
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approve</CardTitle>
        <CardDescription>Generation starts only after you approve.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {blocking.length ? (
          <ul className="flex flex-col gap-2 text-sm" aria-label="Blocking findings">
            {blocking.map((finding) => (
              <li key={finding.id ?? finding.message} className="flex items-start gap-2">
                {finding.overridable && finding.id ? (
                  <input
                    type="checkbox"
                    aria-label={`Override: ${finding.message}`}
                    checked={overrides.includes(finding.id)}
                    onChange={(e) =>
                      setOverrides((o) =>
                        e.target.checked ? [...o, finding.id as string] : o.filter((x) => x !== finding.id),
                      )
                    }
                  />
                ) : (
                  <Badge variant="danger">blocks</Badge>
                )}
                <span>{finding.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-slate-700">No blocking findings.</p>
        )}
        {overrides.length ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor="override-reason">Reason for the override</Label>
            <Textarea id="override-reason" rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
          </div>
        ) : null}
        {nonOverridable.length ? (
          <Alert tone="danger">
            Policy findings cannot be overridden; regenerate the plan with a different instruction.
          </Alert>
        ) : null}
        {approve.error ? (
          <Alert tone="danger">
            {approve.error.message}
            {approve.error instanceof ApiError
              ? approve.error.issues.map((i, n) => <div key={n}>{i.message}</div>)
              : null}
          </Alert>
        ) : null}
        {["approved", "generating", "ready", "partial", "needs_review"].includes(state) ? (
          <p className="text-sm text-slate-700">
            This version is already approved;{" "}
            <a className="text-blue-800 underline" href={`/videos/${videoId}?version=${versionId}`}>
              open it in the studio
            </a>
            .
          </p>
        ) : null}
        <Button
          onClick={() => approve.mutate()}
          disabled={disabled || approve.isPending || needsReason || nonOverridable.length > 0}
        >
          {approve.isPending ? "Approving…" : "Approve and generate"}
        </Button>
      </CardContent>
    </Card>
  );
}

/** "Regenerate plan" (`:replan`): optional instruction, optional fresh memory. */
export function ReplanPanel({
  versionId,
  videoId,
  disabled,
}: {
  versionId: string;
  videoId: string;
  disabled: boolean;
}) {
  const router = useRouter();
  const [instruction, setInstruction] = useState("");
  const [refresh, setRefresh] = useState(false);
  const replan = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/versions/{version_id}:replan", {
          params: { path: { version_id: versionId } },
          headers: idempotencyKey(),
          body: { instruction: instruction.trim() || null, refresh_memory: refresh },
        }),
      ),
    onSuccess: (accepted) =>
      router.push(`/videos/${videoId}/versions/${accepted.version_id}/previz?job=${accepted.job_id}`),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Regenerate plan</CardTitle>
        <CardDescription>A new version from the same request; this one stays.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <Label htmlFor="replan-instruction">What should change? (optional)</Label>
        <Textarea
          id="replan-instruction"
          rows={2}
          value={instruction}
          onChange={(e) => setInstruction(e.target.value)}
          placeholder="make the opening more aggressive"
        />
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={refresh} onChange={(e) => setRefresh(e.target.checked)} />
          Use the creator&apos;s latest memory (otherwise the pinned snapshot is reused)
        </label>
        {replan.error ? <Alert tone="danger">{replan.error.message}</Alert> : null}
        <Button variant="outline" onClick={() => replan.mutate()} disabled={disabled || replan.isPending}>
          Regenerate plan
        </Button>
        <Button variant="ghost" asChild>
          <Link href={`/videos/${videoId}?version=${versionId}#edit`}>Edit in the studio</Link>
        </Button>
      </CardContent>
    </Card>
  );
}
