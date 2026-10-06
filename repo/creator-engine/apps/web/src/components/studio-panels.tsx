"use client";
/**
 * Versions tree, locks and the takes gallery of the Video Studio (§31, §12.6–§12.8). Every change
 * here creates a new version through the API (restore, branch, a lock change, a take selection, a
 * regeneration); resume re-runs failed nodes in place.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Alert, Empty } from "@/components/ui/misc";
import { api, ApiError, idempotencyKey, unwrap } from "@/lib/api";
import {
  flatten,
  type LockRow,
  lockLabel,
  locksBody,
  locksOf,
  RESUMABLE,
  sameLock,
  versionTree,
  type VersionSummary,
} from "@/lib/edits";
import { humanize } from "@/lib/format";
import { keys, useTakes, useVocabulary } from "@/lib/queries";
import { useStudio } from "@/lib/store";

type Json = Record<string, unknown>;

function ErrorText({ error }: { error: Error | null }) {
  if (!error) return null;
  return (
    <Alert tone="danger">
      {error.message}
      {error instanceof ApiError ? error.issues.map((i, n) => <div key={n}>{i.message}</div>) : null}
    </Alert>
  );
}

// ------------------------------------------------------------------------------- versions

export function VersionsPanel({
  videoId,
  versions,
  currentId,
}: {
  videoId: string;
  versions: VersionSummary[];
  currentId: string;
}) {
  const router = useRouter();
  const client = useQueryClient();
  const [compare, setCompare] = useState<string[]>([]);
  const [branchName, setBranchName] = useState("");
  const done = async (versionId: string) => {
    await client.invalidateQueries({ queryKey: keys.versions(videoId) });
    await client.invalidateQueries({ queryKey: keys.video(videoId) });
    router.replace(`/videos/${videoId}?version=${versionId}`);
  };
  const restore = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/v1/versions/{version_id}:restore", {
          params: { path: { version_id: id } },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: (accepted) => done(accepted.version_id),
  });
  const branch = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/versions/{version_id}:branch", {
          params: { path: { version_id: currentId } },
          headers: idempotencyKey(),
          body: { name: branchName.trim() },
        }),
      ),
    onSuccess: async (accepted) => {
      setBranchName("");
      await done(accepted.version_id);
    },
  });
  const resume = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/v1/versions/{version_id}:resume", {
          params: { path: { version_id: id } },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: async (accepted) => {
      await client.invalidateQueries({ queryKey: keys.version(accepted.version_id) });
    },
  });
  const nodes = flatten(versionTree(versions));
  const current = versions.find((v) => v.id === currentId);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Versions</CardTitle>
        <CardDescription>Every change is a new version; nothing is overwritten.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-col gap-1 text-sm" aria-label="Versions">
          {nodes.map(({ version: v, depth }) => (
            <li
              key={v.id}
              className="flex items-center justify-between gap-2"
              style={{ paddingLeft: `${depth * 0.75}rem` }}
            >
              <span className="flex min-w-0 items-center gap-1">
                <input
                  type="checkbox"
                  aria-label={`Compare v${v.number}`}
                  checked={compare.includes(v.id)}
                  onChange={(e) =>
                    setCompare((c) =>
                      e.target.checked ? [...c.filter((x) => x !== v.id), v.id].slice(-2) : c.filter((x) => x !== v.id),
                    )
                  }
                />
                <button
                  type="button"
                  className={v.id === currentId ? "font-semibold" : "text-blue-800 hover:underline"}
                  onClick={() => router.replace(`/videos/${videoId}?version=${v.id}`)}
                >
                  {depth ? "↳ " : ""}v{v.number} · {humanize(v.origin)}
                </button>
                {v.branch !== "main" ? <Badge variant="outline">{v.branch}</Badge> : null}
              </span>
              <span className="flex items-center gap-1">
                <StateBadge state={v.state} />
                {v.id !== currentId ? (
                  <Button size="sm" variant="ghost" onClick={() => restore.mutate(v.id)} disabled={restore.isPending}>
                    Restore
                  </Button>
                ) : null}
                {RESUMABLE.has(v.state) ? (
                  <Button size="sm" variant="ghost" onClick={() => resume.mutate(v.id)} disabled={resume.isPending}>
                    Resume
                  </Button>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
        <div className="flex items-center gap-2">
          {compare.length === 2 ? (
            <Button asChild size="sm" variant="outline" data-testid="compare-versions">
              <Link
                href={(() => {
                  const [a, b] = [...compare].sort(
                    (x, y) =>
                      (versions.find((v) => v.id === x)?.number ?? 0) - (versions.find((v) => v.id === y)?.number ?? 0),
                  );
                  return `/videos/${videoId}/compare?a=${a}&b=${b}`;
                })()}
              >
                Compare the two
              </Link>
            </Button>
          ) : (
            <p className="text-xs text-slate-600">Tick two versions to compare them.</p>
          )}
        </div>
        <form
          className="flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (branchName.trim()) branch.mutate();
          }}
        >
          <Input
            aria-label="Branch name"
            placeholder={`branch from v${current?.number ?? "?"}`}
            value={branchName}
            onChange={(e) => setBranchName(e.target.value.toLowerCase())}
          />
          <Button type="submit" size="sm" variant="outline" disabled={!branchName.trim() || branch.isPending}>
            Branch
          </Button>
        </form>
        <ErrorText error={restore.error ?? branch.error ?? resume.error} />
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------------------- locks

export function LocksPanel({ spec, versionId }: { spec: Json; versionId: string }) {
  const client = useQueryClient();
  const { showProposal } = useStudio();
  const vocab = useVocabulary();
  const current = locksOf(spec);
  const [rows, setRows] = useState<LockRow[]>(current);
  const [group, setGroup] = useState("");
  const [scopeKey, setScopeKey] = useState("");
  const scenes = ((spec.scenes ?? []) as Json[]).map((s) => String(s.key));
  const cast = ((spec.cast ?? []) as Json[]).map((c) => String(c.key));
  const definition = vocab.data?.lock_groups.find((g) => g.group === group);
  const scopeField = definition?.scope_fields.includes("scene_keys")
    ? "scene_keys"
    : definition?.scope_fields.includes("character_keys")
      ? "character_keys"
      : null;
  const changed = rows.length !== current.length || rows.some((r) => !current.some((c) => sameLock(c, r)));
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/v1/versions/{version_id}/locks", {
          params: { path: { version_id: versionId } },
          headers: idempotencyKey(),
          body: locksBody(rows),
        }),
      ),
    onSuccess: async (accepted) => {
      showProposal(accepted.edit_proposal_id);
      await client.invalidateQueries({ queryKey: keys.edits(versionId) });
    },
  });
  const add = () => {
    if (!group) return;
    const scope = { scene_keys: null, character_keys: null, shot_keys: null } as LockRow["scope"];
    if (scopeField && scopeKey) scope[scopeField] = [scopeKey];
    const row: LockRow = { group, scope, setBy: "user" };
    if (!rows.some((r) => sameLock(r, row))) setRows((all) => [...all, row]);
    setGroup("");
    setScopeKey("");
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Locks</CardTitle>
        <CardDescription>
          A locked value cannot change, and locked engines stay pinned; an instruction never removes a lock.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        {rows.length ? (
          <ul aria-label="Locks" className="flex flex-col gap-1">
            {rows.map((r) => (
              <li key={`${r.group}${JSON.stringify(r.scope)}`} className="flex items-center justify-between">
                <span>🔒 {lockLabel(r)}</span>
                <Button size="sm" variant="ghost" onClick={() => setRows((all) => all.filter((x) => !sameLock(x, r)))}>
                  Unlock
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <Empty>Nothing is locked.</Empty>
        )}
        <div className="flex items-center gap-2">
          <Select aria-label="Lock group" value={group} onChange={(e) => setGroup(e.target.value)}>
            <option value="">Lock…</option>
            {(vocab.data?.lock_groups ?? []).map((g) => (
              <option key={g.group} value={g.group}>
                {humanize(g.group)}
              </option>
            ))}
          </Select>
          {scopeField ? (
            <Select aria-label="Lock scope" value={scopeKey} onChange={(e) => setScopeKey(e.target.value)}>
              <option value="">whole video</option>
              {(scopeField === "scene_keys" ? scenes : cast).map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
          ) : null}
          <Button size="sm" variant="outline" onClick={add} disabled={!group}>
            Add
          </Button>
        </div>
        <ErrorText error={save.error} />
        <div>
          <Button
            size="sm"
            onClick={() => save.mutate()}
            disabled={!changed || save.isPending}
            data-testid="save-locks"
          >
            Save locks
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ------------------------------------------------------------------------------- takes

const SHOT_COMPONENTS = ["avatar_video", "keyframe", "camera_post", "broll", "lipsync"] as const;

export function TakesGallery({ spec, versionId }: { spec: Json; versionId: string }) {
  const client = useQueryClient();
  const { showProposal } = useStudio();
  const takes = useTakes(versionId);
  const [component, setComponent] = useState<string>("avatar_video");
  const shots = ((spec.scenes ?? []) as Json[]).flatMap((scene) =>
    ((scene.shots ?? []) as Json[]).map((shot) => ({
      scene: String(scene.key),
      key: String(shot.key),
      type: String(shot.type),
    })),
  );
  const onAccepted = async (accepted: { edit_proposal_id: string }) => {
    showProposal(accepted.edit_proposal_id);
    await client.invalidateQueries({ queryKey: keys.edits(versionId) });
  };
  const select = useMutation({
    mutationFn: ({ shot, take }: { shot: string; take: string }) =>
      unwrap(
        api.POST("/v1/versions/{version_id}/shots/{shot_key}/takes/{take_key}:select", {
          params: { path: { version_id: versionId, shot_key: shot, take_key: take } },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: onAccepted,
  });
  const regenerate = useMutation({
    mutationFn: (shot: string) =>
      unwrap(
        api.POST("/v1/versions/{version_id}/shots/{shot_key}:regenerate", {
          params: { path: { version_id: versionId, shot_key: shot } },
          headers: idempotencyKey(),
          body: { components: [component], seed_policy: "new", strategy: "full" },
        }),
      ),
    onSuccess: onAccepted,
  });
  const byShot = new Map<string, NonNullable<typeof takes.data>["takes"]>();
  for (const take of takes.data?.takes ?? []) byShot.set(take.shot_key, [...(byShot.get(take.shot_key) ?? []), take]);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Shots and takes</CardTitle>
        <CardDescription>
          Ranked takes per shot; selecting one or regenerating a shot creates a new version.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        <div className="flex items-center gap-2">
          <span>Regenerate:</span>
          <Select aria-label="Component to regenerate" value={component} onChange={(e) => setComponent(e.target.value)}>
            {SHOT_COMPONENTS.map((c) => (
              <option key={c} value={c}>
                {humanize(c)}
              </option>
            ))}
          </Select>
        </div>
        {shots.map((shot) => {
          const list = byShot.get(shot.key) ?? [];
          return (
            <div key={shot.key} className="flex flex-col gap-1" data-testid={`shot-${shot.key}`}>
              <div className="flex items-center justify-between">
                <span>
                  <span className="font-mono text-xs">{shot.key}</span> · {humanize(shot.type)} · {shot.scene}
                </span>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => regenerate.mutate(shot.key)}
                  disabled={regenerate.isPending}
                >
                  Regenerate
                </Button>
              </div>
              {list.length ? (
                <ul className="flex flex-wrap gap-2" aria-label={`Takes of ${shot.key}`}>
                  {list.map((take) => (
                    <li
                      key={take.take_key}
                      className="flex w-28 flex-col gap-1 rounded border border-[var(--color-border)] p-1"
                    >
                      {take.video ? (
                        <video
                          src={`${take.video.url}#t=0.5`}
                          muted
                          playsInline
                          preload="metadata"
                          className="aspect-[9/16] w-full bg-black"
                        />
                      ) : (
                        <div className="flex aspect-[9/16] items-center justify-center bg-slate-100 text-xs text-slate-600">
                          no video
                        </div>
                      )}
                      <span className="text-xs">
                        {take.take_key} · rank {take.rank ?? "—"}
                        {take.selected ? (
                          <Badge variant="success" className="ml-1">
                            selected
                          </Badge>
                        ) : null}
                      </span>
                      {!take.selected ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => select.mutate({ shot: shot.key, take: `tk_${take.take_index}` })}
                          disabled={select.isPending}
                        >
                          Select
                        </Button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-slate-600">
                  No takes yet (generated shots have takes once the version is built).
                </p>
              )}
            </div>
          );
        })}
        <ErrorText error={select.error ?? regenerate.error} />
      </CardContent>
    </Card>
  );
}
