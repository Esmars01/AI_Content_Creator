"use client";
/** World Studio (§31, Phase 10): the World DNA editor with a 2D floor plan, the plates gallery
 * (generate, choose, approve), versions with diffs, and the continuity report. Overrides made in a
 * video never change World DNA (I6): making one permanent is a new draft version here. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AssetImage, ErrorNote, JobLine, useStudioJob } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize } from "@/lib/format";
import { keys } from "@/lib/queries";
import { editableDraft, floorPlan, type Json, parseJson, plateCells, WORLD_DNA_SECTIONS } from "@/lib/studio";

type World = {
  id: string;
  versions: { id: string; number: number; status: string }[];
  current_version_id: string | null;
};

function useVersion(versionId: string | null) {
  return useQuery({
    queryKey: keys.worldVersion(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(
        api.GET("/v1/world-versions/{world_version_id}", { params: { path: { world_version_id: versionId ?? "" } } }),
      ),
  });
}

export function FloorPlan({ dna, size = 320 }: { dna: Json; size?: number }) {
  const items = floorPlan(dna, size);
  const colors = { zone: "#cbd5e1", element: "#1e40af", camera: "#b45309" } as const;
  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label="Floor plan"
      className="rounded border border-slate-300 bg-slate-50"
    >
      {items.map((item) =>
        item.kind === "zone" ? (
          <circle key={item.key} cx={item.x} cy={item.y} r={18} fill={colors.zone} opacity={0.6} />
        ) : item.kind === "camera" ? (
          <g key={item.key}>
            <polygon
              points={`${item.x},${item.y - 8} ${item.x - 7},${item.y + 6} ${item.x + 7},${item.y + 6}`}
              fill={item.status === "forbidden" ? "#991b1b" : colors.camera}
            />
            <text x={item.x + 9} y={item.y + 4} fontSize={10} fill="#78350f">
              {item.label}
            </text>
          </g>
        ) : (
          <g key={item.key}>
            <rect x={item.x - 5} y={item.y - 5} width={10} height={10} fill={colors.element} />
            <text x={item.x + 8} y={item.y + 4} fontSize={10} fill="#1e3a8a">
              {item.label}
            </text>
          </g>
        ),
      )}
    </svg>
  );
}

export function WorldDnaPanel({ world }: { world: World }) {
  const client = useQueryClient();
  const draftRef = editableDraft(world.versions);
  const shownId = draftRef?.id ?? world.current_version_id;
  const version = useVersion(shownId);
  const [section, setSection] = useState<(typeof WORLD_DNA_SECTIONS)[number]>("elements");
  const [text, setText] = useState<string | null>(null);
  const refresh = () => client.invalidateQueries({ queryKey: keys.world(world.id) });
  const start = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/worlds/{world_id}/versions", { params: { path: { world_id: world.id } }, body: {} })),
    onSuccess: refresh,
  });
  const save = useMutation({
    mutationFn: (dna: Json) =>
      unwrap(
        api.PATCH("/v1/world-versions/{world_version_id}", {
          params: { path: { world_version_id: draftRef?.id ?? "" } },
          body: { dna: dna as never },
        }),
      ),
    onSuccess: async () => {
      setText(null);
      await client.invalidateQueries({ queryKey: keys.worldVersion(draftRef?.id ?? "") });
    },
  });
  if (!version.data) return shownId ? <Skeleton className="h-64" /> : <Empty>No version.</Empty>;
  const dna = version.data.dna as Json;
  const parsed = text === null ? null : parseJson(text);
  const editing = Boolean(draftRef);
  const positions = (dna.camera_positions ?? []) as Json[];
  return (
    <div className="grid grid-cols-[auto_1fr] gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Floor plan</CardTitle>
          <CardDescription>Elements (blue), zones, camera positions (amber).</CardDescription>
        </CardHeader>
        <CardContent>
          <FloorPlan dna={dna} />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>
            World DNA — version {version.data.number} ({humanize(version.data.status)})
          </CardTitle>
          <CardDescription>
            {editing ? "Editing the draft." : "Approved versions are immutable; start a draft to edit."}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {!editing ? (
            <div>
              <Button onClick={() => start.mutate()} disabled={start.isPending}>
                Start a draft
              </Button>
            </div>
          ) : null}
          <Table>
            <thead>
              <tr>
                <Th>Camera position</Th>
                <Th>Status</Th>
                <Th>Framing</Th>
                <Th>Profiles</Th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={String(p.key)}>
                  <Td>{String(p.key)}</Td>
                  <Td>
                    <Badge variant={p.status === "forbidden" ? "danger" : "success"}>
                      {String(p.status ?? "permitted")}
                    </Badge>
                  </Td>
                  <Td className="text-xs">{String(p.default_framing ?? "")}</Td>
                  <Td className="text-xs">{((p.allowed_camera_profiles ?? []) as string[]).join(", ")}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
          <div className="flex items-center gap-2">
            <Label htmlFor="world-section">Section</Label>
            <Select
              id="world-section"
              value={section}
              onChange={(e) => {
                setSection(e.target.value as typeof section);
                setText(null);
              }}
            >
              {WORLD_DNA_SECTIONS.map((s) => (
                <option key={s} value={s}>
                  {humanize(s)}
                </option>
              ))}
            </Select>
          </div>
          <Textarea
            aria-label={`${section} JSON`}
            rows={14}
            className="font-mono text-xs"
            readOnly={!editing}
            value={text ?? JSON.stringify(dna[section] ?? null, null, 2)}
            onChange={(e) => setText(e.target.value)}
          />
          {parsed?.error ? <span className="text-xs text-red-800">invalid JSON: {parsed.error}</span> : null}
          {editing ? (
            <div>
              <Button
                onClick={() => parsed?.value !== undefined && save.mutate({ ...dna, [section]: parsed.value })}
                disabled={!parsed || Boolean(parsed.error) || save.isPending}
              >
                Save draft
              </Button>
            </div>
          ) : null}
          <ErrorNote error={start.error ?? save.error} />
        </CardContent>
      </Card>
    </div>
  );
}

export function PlatesPanel({ world }: { world: World }) {
  const client = useQueryClient();
  const draftRef = editableDraft(world.versions);
  const versionId = draftRef?.id ?? world.current_version_id;
  const version = useVersion(versionId);
  const job = useStudioJob([keys.worldVersion(versionId ?? "")]);
  const generate = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/world-versions/{world_version_id}/plates:generate", {
          params: { path: { world_version_id: versionId ?? "" } },
          body: {} as never,
        }),
      ),
    onSuccess: (data) => job.setJobId(data.job_id),
  });
  const choose = useMutation({
    mutationFn: (choice: { position: string; time: string; weather: string; asset: string }) =>
      unwrap(
        api.POST("/v1/world-versions/{world_version_id}/plates:choose", {
          params: { path: { world_version_id: versionId ?? "" } },
          body: {
            camera_position_key: choice.position,
            time_of_day: choice.time,
            weather: choice.weather,
            asset_id: choice.asset,
          },
        }),
      ),
    onSuccess: (data) => {
      if (data.fingerprint_job_id) job.setJobId(data.fingerprint_job_id);
      void client.invalidateQueries({ queryKey: keys.worldVersion(versionId ?? "") });
    },
  });
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/world-versions/{world_version_id}:approve", {
          params: { path: { world_version_id: versionId ?? "" } },
        }),
      ),
    // the world (its versions) and the version this panel shows: its status decides draft or frozen
    onSuccess: () =>
      Promise.all([
        client.invalidateQueries({ queryKey: keys.world(world.id) }),
        client.invalidateQueries({ queryKey: keys.worldVersion(versionId ?? "") }),
      ]),
  });
  if (!version.data) return <Skeleton className="h-48" />;
  const draft = version.data.status === "draft";
  const cells = plateCells(version.data.plate_candidates as Json, version.data.plates as Json);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Plates — version {version.data.number} ({humanize(version.data.status)})
        </CardTitle>
        <CardDescription>
          Empty-world plates per camera position × time of day × weather. The default time and weather of every
          permitted position need a chosen plate and its fingerprint before approval.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {draft ? (
          <div className="flex gap-2">
            <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
              Generate candidates
            </Button>
            <Button variant="outline" onClick={() => approve.mutate()} disabled={approve.isPending}>
              Approve world version
            </Button>
          </div>
        ) : (
          <Alert tone="info">Approved: plates are frozen. Start a draft in the DNA tab to change them.</Alert>
        )}
        <JobLine status={job.status} job={job.job} />
        <ErrorNote error={generate.error ?? choose.error ?? approve.error} />
        <p className="text-sm">Fingerprints: {version.data.fingerprints_artifact_id ? "computed" : "not computed"}</p>
        {cells.length ? (
          cells.map((cell) => (
            <section key={`${cell.position}-${cell.time}-${cell.weather}`}>
              <h3 className="mb-1 text-sm font-medium">
                {cell.position} · {humanize(cell.time)} · {cell.weather}
              </h3>
              <div className="flex flex-wrap gap-2">
                {[...new Set([...(cell.chosen ? [cell.chosen] : []), ...cell.candidates])].map((asset) => (
                  <button
                    key={asset}
                    type="button"
                    disabled={!draft}
                    className={`rounded border-2 ${cell.chosen === asset ? "border-blue-700" : "border-transparent"}`}
                    onClick={() =>
                      choose.mutate({ position: cell.position, time: cell.time, weather: cell.weather, asset })
                    }
                    aria-label={cell.chosen === asset ? "Chosen plate" : "Choose this plate"}
                  >
                    <AssetImage assetId={asset} alt="world plate" className="h-40 w-24 rounded object-cover" />
                  </button>
                ))}
              </div>
            </section>
          ))
        ) : (
          <Empty>No plates yet.</Empty>
        )}
      </CardContent>
    </Card>
  );
}

export function WorldVersionsPanel({ world }: { world: World }) {
  const [selected, setSelected] = useState<string | null>(world.versions[world.versions.length - 1]?.id ?? null);
  const diff = useQuery({
    queryKey: ["world-diff", selected ?? ""],
    enabled: Boolean(selected),
    queryFn: () =>
      unwrap(
        api.GET("/v1/world-versions/{world_version_id}/diff", {
          params: { path: { world_version_id: selected ?? "" } },
        }),
      ),
    retry: false,
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Versions</CardTitle>
        <CardDescription>Each diff is against the parent version.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-wrap gap-2 text-sm">
          {world.versions.map((v) => (
            <li key={v.id}>
              <Button size="sm" variant={selected === v.id ? "default" : "outline"} onClick={() => setSelected(v.id)}>
                v{v.number} · {v.status}
              </Button>
            </li>
          ))}
        </ul>
        {diff.error ? <ErrorNote error={diff.error} /> : null}
        {diff.data ? (
          diff.data.changes.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Path</Th>
                  <Th>Before</Th>
                  <Th>After</Th>
                </tr>
              </thead>
              <tbody>
                {diff.data.changes.map((c, i) => (
                  <tr key={i}>
                    <Td className="font-mono text-xs">{String((c as Json).path ?? "")}</Td>
                    <Td className="font-mono text-xs">{JSON.stringify((c as Json).before ?? null)}</Td>
                    <Td className="font-mono text-xs">{JSON.stringify((c as Json).after ?? null)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>No changes against the parent.</Empty>
          )
        ) : null}
      </CardContent>
    </Card>
  );
}

export function ContinuityPanel({ worldId }: { worldId: string }) {
  const continuity = useQuery({
    queryKey: ["world-continuity", worldId],
    queryFn: () => unwrap(api.GET("/v1/worlds/{world_id}/continuity", { params: { path: { world_id: worldId } } })),
  });
  if (!continuity.data) return <Skeleton className="h-32" />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Continuity</CardTitle>
        <CardDescription>{continuity.data.note}</CardDescription>
      </CardHeader>
      <CardContent>
        {continuity.data.uses.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Video</Th>
                <Th>Version</Th>
                <Th>Scene</Th>
                <Th>Binding</Th>
              </tr>
            </thead>
            <tbody>
              {continuity.data.uses.map((u) => (
                <tr key={`${u.version_id}-${u.scene_key}`}>
                  <Td>{u.video_title || u.video_id}</Td>
                  <Td>
                    v{u.version_number} · {humanize(u.version_state)}
                  </Td>
                  <Td>{u.scene_key}</Td>
                  <Td className="text-xs">
                    {u.camera_position_key} · {humanize(u.time_of_day ?? "")} · {u.weather}
                    {u.has_overrides ? " · overrides" : ""}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty>Not used in a video yet.</Empty>
        )}
      </CardContent>
    </Card>
  );
}
