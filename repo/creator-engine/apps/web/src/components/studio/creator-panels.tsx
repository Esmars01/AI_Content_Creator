"use client";
/** Creator Studio tabs (§31, Phase 10): Creator DNA editor, Creator Memory, Appearance (identity pack),
 * Voice (design, test bench, lexicon), Wardrobe, Creator Test and Consent. Every model action is a
 * studio job; edits only touch drafts; approval is explicit. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";

import { RoleNote } from "@/components/role-note";
import {
  ArtifactAudio,
  AssetAudio,
  AssetImage,
  ErrorNote,
  JobLine,
  MockBadge,
  useStudioJob,
} from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Tabs, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { embeddingStatus, type MemorySource, memoryProvenance } from "@/lib/phase12";
import { keys, useCreator, useMemory, useVoices, useWorlds } from "@/lib/queries";
import { useCan } from "@/lib/roles";
import {
  CREATOR_DNA_TABS,
  coverageSummary,
  editableDraft,
  fieldKind,
  groupPack,
  type Json,
  memoryActions,
  memoryGroups,
  type PackImage,
  parseJson,
  scorecardRows,
  sectionKey,
} from "@/lib/studio";

type Creator = {
  id: string;
  versions: { id: string; number: number; status: string }[];
  current_version_id: string | null;
};

// ====================================================================== Creator DNA editor
function SectionFields({ value, onChange }: { value: Json; onChange: (next: Json) => void }) {
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  return (
    <div className="grid grid-cols-1 items-start gap-x-3 gap-y-2 sm:grid-cols-[14rem_1fr]">
      {Object.entries(value).map(([field, current]) => {
        const kind = fieldKind(current);
        const id = `dna-${field}`;
        return (
          <div key={field} className="contents">
            <Label htmlFor={id} className="pt-2">
              {humanize(field)}
            </Label>
            {kind === "number" ? (
              <Input
                id={id}
                type="number"
                step="0.05"
                value={String(current)}
                onChange={(e) => onChange({ ...value, [field]: Number(e.target.value) })}
              />
            ) : kind === "string" ? (
              <Input
                id={id}
                value={String(current)}
                onChange={(e) => onChange({ ...value, [field]: e.target.value })}
              />
            ) : kind === "boolean" ? (
              <input
                id={id}
                type="checkbox"
                className="mt-2 h-4 w-4"
                checked={Boolean(current)}
                onChange={(e) => onChange({ ...value, [field]: e.target.checked })}
              />
            ) : (
              <div className="flex flex-col gap-1">
                <Textarea
                  id={id}
                  rows={4}
                  className="font-mono text-xs"
                  value={drafts[field] ?? JSON.stringify(current, null, 2)}
                  onChange={(e) => {
                    setDrafts({ ...drafts, [field]: e.target.value });
                    const parsed = parseJson(e.target.value);
                    if (parsed.error === undefined) onChange({ ...value, [field]: parsed.value });
                  }}
                />
                {drafts[field] !== undefined && parseJson(drafts[field] ?? "").error ? (
                  <span className="text-xs text-red-800">invalid JSON: {parseJson(drafts[field] ?? "").error}</span>
                ) : null}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

type VersionLinks = { appearance: string; voice: string; wardrobes: string[]; worlds: string[] };
type LinkedVersion = {
  appearance_version_id?: string | null;
  voice_version_id?: string | null;
  default_wardrobe_version_ids?: string[];
  default_world_ids?: string[];
};

/** What a creator draft points at (§12.8): its look, voice, default outfits and worlds. A creator
 * version pins them, and approving a new look or voice only updates that asset — videos use it once
 * an approved creator version links it. Before this card nothing in the Studio could set a link, so a
 * new voice or look never reached a video and a new creator could never be approved (audit CR-VOICE). */
export function IdentityLinks({
  creatorId,
  versionId,
  version,
}: {
  creatorId: string;
  versionId: string;
  version: LinkedVersion;
}) {
  const client = useQueryClient();
  const can = useCan("write_content");
  const appearances = useQuery({
    queryKey: ["appearances", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/appearances", { params: { path: { creator_id: creatorId } } })),
  });
  const wardrobes = useQuery({
    queryKey: ["wardrobes", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/wardrobes", { params: { path: { creator_id: creatorId } } })),
  });
  const voices = useVoices(creatorId);
  const worlds = useWorlds();
  const saved: VersionLinks = {
    appearance: version.appearance_version_id ?? "",
    voice: version.voice_version_id ?? "",
    wardrobes: version.default_wardrobe_version_ids ?? [],
    worlds: version.default_world_ids ?? [],
  };
  const [edited, setEdited] = useState<VersionLinks | null>(null);
  const links = edited ?? saved;
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/v1/creator-versions/{creator_version_id}", {
          params: { path: { creator_version_id: versionId } },
          body: {
            appearance_version_id: links.appearance || null,
            voice_version_id: links.voice || null,
            defaults: { world_ids: links.worlds, wardrobe_version_ids: links.wardrobes },
          },
        }),
      ),
    onSuccess: async () => {
      setEdited(null);
      await client.invalidateQueries({ queryKey: ["creator-version", versionId] });
    },
  });
  const looks = (appearances.data ?? []).flatMap((a) =>
    (a.versions as { id: string; number: number; status: string }[])
      .filter((v) => v.status === "approved")
      .map((v) => ({ id: v.id, label: `${a.name} · version ${v.number}` })),
  );
  const voiceOptions = (voices.data ?? [])
    .filter((v) => v.current_version_id)
    .map((v) => ({ id: String(v.current_version_id), label: `${v.name} (its current version)` }));
  if (links.voice && !voiceOptions.some((o) => o.id === links.voice))
    voiceOptions.unshift({ id: links.voice, label: "The linked version (not its voice's current version)" });
  const outfits = (wardrobes.data ?? []).flatMap((w) =>
    (w.versions as { id: string; number: number; status: string }[])
      .filter((v) => v.status === "approved")
      .map((v) => ({ id: v.id, label: `${w.name} · version ${v.number}` })),
  );
  const approvedWorlds = (worlds.data?.items ?? []).filter((w) => w.current_version_id);
  const set = (change: Partial<VersionLinks>) => setEdited({ ...links, ...change });
  const toggle = (list: string[], id: string, on: boolean) => (on ? [...list, id] : list.filter((x) => x !== id));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Identity links</CardTitle>
        <CardDescription>
          The look, voice, default outfits and worlds this draft uses. New videos use them once this version is
          approved.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <RoleNote />
        <div className="grid gap-3 md:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="link-appearance">Appearance</Label>
            <Select id="link-appearance" value={links.appearance} onChange={(e) => set({ appearance: e.target.value })}>
              <option value="">None linked</option>
              {looks.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="link-voice">Voice</Label>
            <Select id="link-voice" value={links.voice} onChange={(e) => set({ voice: e.target.value })}>
              <option value="">None linked</option>
              {voiceOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.label}
                </option>
              ))}
            </Select>
          </div>
        </div>
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">Default outfits</legend>
          {outfits.length ? (
            outfits.map((o) => (
              <label key={o.id} className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={links.wardrobes.includes(o.id)}
                  onChange={(e) => set({ wardrobes: toggle(links.wardrobes, o.id, e.target.checked) })}
                />
                {o.label}
              </label>
            ))
          ) : (
            <p className="text-sm text-slate-600">No approved outfits yet (Wardrobe tab).</p>
          )}
        </fieldset>
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">Default worlds</legend>
          {approvedWorlds.map((w) => (
            <label key={w.id} className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={links.worlds.includes(w.id)}
                onChange={(e) => set({ worlds: toggle(links.worlds, w.id, e.target.checked) })}
              />
              {w.name}
            </label>
          ))}
        </fieldset>
        {!links.appearance || !links.voice ? (
          <p className="text-sm text-slate-700">Approval needs an approved appearance and voice linked.</p>
        ) : null}
        <div>
          <Button onClick={() => save.mutate()} disabled={!edited || save.isPending || !can.allowed} title={can.reason}>
            Save links
          </Button>
        </div>
        <ErrorNote error={save.error} />
      </CardContent>
    </Card>
  );
}

export function DnaEditor({ creator }: { creator: Creator }) {
  const client = useQueryClient();
  const can = useCan("write_content");
  const draftRef = editableDraft(creator.versions);
  const [tab, setTab] = useState<(typeof CREATOR_DNA_TABS)[number]["id"]>("identity");
  const [advanced, setAdvanced] = useState(false);
  const [edited, setEdited] = useState<Json | null>(null);
  const [attest, setAttest] = useState(false);
  const draft = useQuery({
    queryKey: ["creator-version", draftRef?.id ?? ""],
    enabled: Boolean(draftRef),
    queryFn: () =>
      unwrap(
        api.GET("/v1/creator-versions/{creator_version_id}", {
          params: { path: { creator_version_id: draftRef?.id ?? "" } },
        }),
      ),
  });
  const refresh = () => client.invalidateQueries({ queryKey: keys.creator(creator.id) });
  const start = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creators/{creator_id}/versions", {
          params: { path: { creator_id: creator.id } },
          body: { from_version_id: creator.current_version_id },
        }),
      ),
    onSuccess: refresh,
  });
  const save = useMutation({
    mutationFn: (dna: Json) =>
      unwrap(
        api.PATCH("/v1/creator-versions/{creator_version_id}", {
          params: { path: { creator_version_id: draftRef?.id ?? "" } },
          body: { dna: dna as never },
        }),
      ),
    onSuccess: async () => {
      setEdited(null);
      await client.invalidateQueries({ queryKey: ["creator-version", draftRef?.id ?? ""] });
    },
  });
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creator-versions/{creator_version_id}:approve", {
          params: { path: { creator_version_id: draftRef?.id ?? "" } },
          body: { attest_adult_presentation: attest },
        }),
      ),
    onSuccess: refresh,
  });
  if (!draftRef) {
    return (
      <Card>
        <CardContent className="flex flex-col gap-2 pt-4">
          <p className="text-sm">The current version is approved and immutable. Edits happen on a new draft.</p>
          <div>
            <Button onClick={() => start.mutate()} disabled={start.isPending || !can.allowed} title={can.reason}>
              Start a draft
            </Button>
          </div>
          <ErrorNote error={start.error} />
        </CardContent>
      </Card>
    );
  }
  if (!draft.data) return <Skeleton className="h-64" />;
  const dna = (edited ?? (draft.data.dna as unknown as Json)) as Json;
  const tabDef = CREATOR_DNA_TABS.find((t) => t.id === tab) ?? CREATOR_DNA_TABS[0];
  const key = sectionKey(dna, tabDef.keys);
  const section = (dna[key] ?? {}) as Json;
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Creator DNA — draft version {draftRef.number}</CardTitle>
          <CardDescription>
            Tendencies, targets and bounds — never engine controls (I1). Approval needs the owner&apos;s adult
            attestation.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Tabs tabs={CREATOR_DNA_TABS} value={tab} onChange={setTab} label="Creator DNA sections" />
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} /> Advanced (JSON)
          </label>
          {advanced ? (
            <Textarea
              aria-label={`${tabDef.label} JSON`}
              rows={14}
              className="font-mono text-xs"
              defaultValue={JSON.stringify(section, null, 2)}
              key={`${key}-${draft.dataUpdatedAt}`}
              onChange={(e) => {
                const parsed = parseJson(e.target.value);
                if (parsed.error === undefined) setEdited({ ...dna, [key]: parsed.value as Json });
              }}
            />
          ) : (
            <SectionFields value={section} onChange={(next) => setEdited({ ...dna, [key]: next })} />
          )}
          <div className="flex flex-wrap items-center gap-2">
            <Button
              onClick={() => save.mutate(dna)}
              disabled={!edited || save.isPending || !can.allowed}
              title={can.reason}
            >
              Save draft
            </Button>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={attest} onChange={(e) => setAttest(e.target.checked)} />I attest this
              synthetic creator presents as an adult
            </label>
            <Button
              variant="outline"
              onClick={() => approve.mutate()}
              disabled={!attest || Boolean(edited) || approve.isPending || !can.allowed}
              title={can.reason}
            >
              Approve version {draftRef.number}
            </Button>
          </div>
          <ErrorNote error={save.error ?? approve.error} />
          {approve.isSuccess ? (
            <Alert tone="success">Approved: this version is now the creator&apos;s current version.</Alert>
          ) : null}
        </CardContent>
      </Card>
      <IdentityLinks creatorId={creator.id} versionId={draftRef.id} version={draft.data as LinkedVersion} />
    </div>
  );
}

// ====================================================================== Creator Memory
type MemoryItem = {
  id: string;
  category: string;
  kind: string;
  status: string;
  pinned: boolean;
  conflict_state: string;
  conflict_ids: string[];
  text: string | null;
  confidence: number;
  source: MemorySource;
  evidence_count: number;
  last_seen_at: string;
  embedding_model: string | null;
  creator_version_from: string | null;
  creator_version_to: string | null;
};

/** "Keep both" with creator-version scopes (§18.7): this item holds before `split`, the
 * conflicting ones from `split` on — both stay true, each for its own creator versions. */
function ScopedKeep({
  item,
  versions,
  onKeep,
}: {
  item: MemoryItem;
  versions: { id: string; number: number }[];
  onKeep: (scopes: Record<string, { creator_version_from?: string; creator_version_to?: string }>) => void;
}) {
  const [split, setSplit] = useState("");
  if (!versions.length) return null;
  return (
    <span className="flex items-center gap-1">
      <Select
        aria-label="Split at creator version"
        className="h-8 w-32 text-xs"
        value={split}
        onChange={(e) => setSplit(e.target.value)}
      >
        <option value="">Split at…</option>
        {versions.map((v) => (
          <option key={v.id} value={v.id}>
            version {v.number}
          </option>
        ))}
      </Select>
      <Button
        size="sm"
        variant="outline"
        disabled={!split}
        onClick={() =>
          onKeep({
            [item.id]: { creator_version_to: split },
            ...Object.fromEntries(item.conflict_ids.map((id) => [id, { creator_version_from: split }])),
          })
        }
      >
        Keep both, scoped
      </Button>
    </span>
  );
}

/**
 * Authors a memory item of the two kinds people add by hand most: a fact the creator keeps, and a
 * topic the creator avoids (other kinds stay with the API). The panel promised "or authored" with
 * no way to author (audit MEM-CRUD).
 */
export function AddMemory({ creatorId }: { creatorId: string }) {
  const client = useQueryClient();
  const can = useCan("write_content");
  const [kind, setKind] = useState<"persona_fact" | "avoidance.topic">("persona_fact");
  const [fact, setFact] = useState({ subject: "", predicate: "", object: "" });
  const [topic, setTopic] = useState("");
  const value = kind === "persona_fact" ? fact : { topic: topic.trim() };
  const text =
    kind === "persona_fact" ? `${fact.subject} ${fact.predicate} ${fact.object}`.trim() : `Avoids: ${topic.trim()}`;
  const complete =
    kind === "persona_fact" ? Boolean(fact.subject && fact.predicate && fact.object) : Boolean(topic.trim());
  const add = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creators/{creator_id}/memory", {
          params: { path: { creator_id: creatorId } },
          body: { kind, value, text } as never,
        }),
      ),
    onSuccess: async () => {
      setFact({ subject: "", predicate: "", object: "" });
      setTopic("");
      await client.invalidateQueries({ queryKey: keys.memory(creatorId) });
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add to memory</CardTitle>
        <CardDescription>Authored items are used when planning this creator&apos;s next videos.</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-wrap items-end gap-2 text-sm"
          onSubmit={(e) => {
            e.preventDefault();
            if (complete && can.allowed) add.mutate();
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="memory-kind">Kind</Label>
            <Select id="memory-kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
              <option value="persona_fact">A fact about the creator</option>
              <option value="avoidance.topic">A topic the creator avoids</option>
            </Select>
          </div>
          {kind === "persona_fact" ? (
            (["subject", "predicate", "object"] as const).map((field) => (
              <div key={field} className="flex flex-col gap-1">
                <Label htmlFor={`memory-${field}`}>{humanize(field)}</Label>
                <Input
                  id={`memory-${field}`}
                  value={fact[field]}
                  placeholder={{ subject: "Alex", predicate: "owns", object: "a grey cat" }[field]}
                  onChange={(e) => setFact({ ...fact, [field]: e.target.value })}
                />
              </div>
            ))
          ) : (
            <div className="flex flex-col gap-1">
              <Label htmlFor="memory-topic">Topic</Label>
              <Input id="memory-topic" value={topic} onChange={(e) => setTopic(e.target.value)} />
            </div>
          )}
          <Button type="submit" disabled={!complete || add.isPending || !can.allowed} title={can.reason}>
            Add
          </Button>
        </form>
        <ErrorNote error={add.error} />
      </CardContent>
    </Card>
  );
}

export function MemoryPanel({ creatorId }: { creatorId: string }) {
  const client = useQueryClient();
  const memory = useMemory(creatorId);
  const creator = useCreator(creatorId);
  const versions = ((creator.data as { versions?: { id: string; number: number }[] } | undefined)?.versions ?? []).map(
    (v) => ({ id: v.id, number: v.number }),
  );
  const act = useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) =>
      unwrap(
        api.PATCH("/v1/memory-items/{memory_item_id}", {
          params: { path: { memory_item_id: id } },
          body: body as never,
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.memory(creatorId) }),
  });
  const remove = useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE("/v1/memory-items/{memory_item_id}", { params: { path: { memory_item_id: id } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.memory(creatorId) }),
  });
  const can = useCan("write_content");
  const items = useMemo(() => (memory.data?.items ?? []) as unknown as MemoryItem[], [memory.data]);
  const groups = useMemo(() => memoryGroups(items), [items]);
  if (memory.isLoading) return <Skeleton className="h-48" />;
  if (!items.length)
    return (
      <div className="flex flex-col gap-4">
        <AddMemory creatorId={creatorId} />
        <Empty>No memory items yet: they are proposed after approvals and exports, or authored above.</Empty>
      </div>
    );
  return (
    <div className="flex flex-col gap-4">
      <AddMemory creatorId={creatorId} />
      <ErrorNote error={act.error ?? remove.error} />
      {groups.map(([category, list]) => (
        <Card key={category}>
          <CardHeader>
            <CardTitle>{humanize(category)}</CardTitle>
          </CardHeader>
          <CardContent>
            <Table>
              <thead>
                <tr>
                  <Th>Item</Th>
                  <Th>Status</Th>
                  <Th>Source</Th>
                  <Th>Confidence</Th>
                  <Th>Last seen</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {list.map((item) => (
                  <tr key={item.id}>
                    <Td>{item.text || humanize(item.kind)}</Td>
                    <Td className="text-xs">
                      {humanize(item.status)}
                      {item.pinned ? (
                        <Badge variant="info" className="ml-1">
                          pinned
                        </Badge>
                      ) : null}
                      {item.conflict_state === "unresolved" ? (
                        <Badge variant="danger" className="ml-1">
                          conflict
                        </Badge>
                      ) : null}
                    </Td>
                    <Td className="text-xs">
                      <MemoryProvenance item={item} />
                    </Td>
                    <Td>{item.confidence.toFixed(2)}</Td>
                    <Td className="text-xs">{when(item.last_seen_at)}</Td>
                    <Td className="flex flex-wrap gap-1">
                      {memoryActions(item).map((action) =>
                        action === "resolve_conflict" ? (
                          <span key={action} className="flex gap-1">
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act.mutate({ id: item.id, body: { action, keep: item.id } })}
                            >
                              Keep this
                            </Button>
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => act.mutate({ id: item.id, body: { action, keep: "both" } })}
                            >
                              Keep both
                            </Button>
                            <ScopedKeep
                              item={item}
                              versions={versions}
                              onKeep={(scopes) => act.mutate({ id: item.id, body: { action, keep: "both", scopes } })}
                            />
                          </span>
                        ) : (
                          <Button
                            key={action}
                            size="sm"
                            variant="outline"
                            onClick={() => act.mutate({ id: item.id, body: { action } })}
                          >
                            {humanize(action)}
                          </Button>
                        ),
                      )}
                      {item.status === "active" ? (
                        <Select
                          aria-label="Supersede by"
                          className="h-8 w-40 text-xs"
                          value=""
                          onChange={(e) =>
                            e.target.value &&
                            act.mutate({ id: item.id, body: { action: "supersede", by: e.target.value } })
                          }
                        >
                          <option value="">Supersede by…</option>
                          {list
                            .filter((o) => o.id !== item.id && o.status === "active")
                            .map((o) => (
                              <option key={o.id} value={o.id}>
                                {o.text || o.kind}
                              </option>
                            ))}
                        </Select>
                      ) : null}
                      {item.status !== "forgotten" ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={remove.isPending || !can.allowed}
                          title={can.reason}
                          onClick={() => {
                            if (window.confirm(`Delete “${item.text || humanize(item.kind)}” from memory?`))
                              remove.mutate(item.id);
                          }}
                        >
                          Delete
                        </Button>
                      ) : null}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function MemoryProvenance({ item }: { item: MemoryItem }) {
  const p = memoryProvenance(item.source);
  const embedded = embeddingStatus(item.embedding_model);
  return (
    <div className="flex flex-col gap-0.5">
      <span>
        {p.label}
        {p.videoId ? (
          <>
            {" "}
            (
            <Link className="text-blue-800 underline" href={`/videos/${p.videoId}`}>
              video
            </Link>
            )
          </>
        ) : null}
      </span>
      <span className="text-slate-600">
        {item.evidence_count} evidence{p.videos ? ` · ${p.videos} video(s)` : ""}
      </span>
      {p.mock ? (
        <Badge variant="warning" title="Measured on mock engines: never promoted automatically">
          mock evidence
        </Badge>
      ) : null}
      {item.creator_version_from || item.creator_version_to ? (
        <span className="text-slate-600">scoped to some creator versions</span>
      ) : null}
      <span className={embedded.indexed ? "text-slate-600" : "text-amber-800"}>{embedded.label}</span>
    </div>
  );
}

// ====================================================================== Appearance: the identity pack
export function AppearancePanel({ creatorId }: { creatorId: string }) {
  const client = useQueryClient();
  const appearances = useQuery({
    queryKey: ["appearances", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/appearances", { params: { path: { creator_id: creatorId } } })),
  });
  const [age, setAge] = useState(30);
  const [hair, setHair] = useState("");
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creators/{creator_id}/appearances", {
          params: { path: { creator_id: creatorId } },
          body: { name: "Default", dna: { age_appearance: age, hair } as never },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["appearances", creatorId] }),
  });
  if (appearances.isLoading) return <Skeleton className="h-48" />;
  const list = appearances.data ?? [];
  if (!list.length) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>New appearance</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="age">Apparent age (≥ 18)</Label>
            <Input
              id="age"
              type="number"
              min={18}
              value={age}
              onChange={(e) => setAge(Number(e.target.value))}
              className="w-28"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="hair">Hair</Label>
            <Input id="hair" value={hair} onChange={(e) => setHair(e.target.value)} className="w-56" />
          </div>
          <Button onClick={() => create.mutate()} disabled={age < 18 || create.isPending}>
            Create
          </Button>
          <ErrorNote error={create.error} />
        </CardContent>
      </Card>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {list.map((appearance) => {
        const versions = appearance.versions as { id: string; number: number; status: string }[];
        const latest = versions[versions.length - 1];
        return latest ? (
          <IdentityPack key={appearance.id} appearanceId={appearance.id} versionId={latest.id} name={appearance.name} />
        ) : null;
      })}
    </div>
  );
}

/** The VLM age check in words, leaving out what was not measured (it used to print "via undefined"). */
export function ageCheckText(checks: Json): string {
  const threshold = String(checks.threshold ?? 25);
  const estimate =
    checks.vlm_estimate === null || checks.vlm_estimate === undefined
      ? `VLM apparent age: not measured yet (threshold ${threshold})`
      : `VLM apparent age ${String(checks.vlm_estimate)} (threshold ${threshold})` +
        (checks.vlm_adapter ? ` via ${String(checks.vlm_adapter)}` : "") +
        (checks.vlm_mock ? " — mock answer, not evidence" : "");
  const dna =
    checks.dna_age_appearance !== null && checks.dna_age_appearance !== undefined
      ? `; DNA age ${String(checks.dna_age_appearance)}`
      : "";
  return `${estimate}${dna}.`;
}

function IdentityPack({ appearanceId, versionId, name }: { appearanceId: string; versionId: string; name: string }) {
  const client = useQueryClient();
  const packKey = ["identity-pack", versionId];
  const pack = useQuery({
    queryKey: packKey,
    queryFn: () =>
      unwrap(
        api.GET("/v1/appearance-versions/{appearance_version_id}/identity-pack", {
          params: { path: { appearance_version_id: versionId } },
        }),
      ),
  });
  const job = useStudioJob([packKey]);
  const [decisions, setDecisions] = useState<Record<string, "approved" | "rejected">>({});
  const generate = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/appearance-versions/{appearance_version_id}/identity-pack:generate", {
          params: { path: { appearance_version_id: versionId } },
          body: { candidates: 8 },
        }),
      ),
    onSuccess: (data) => job.setJobId(data.job_id),
  });
  const choose = useMutation({
    mutationFn: (assetId: string) =>
      unwrap(
        api.POST("/v1/appearance-versions/{appearance_version_id}/identity-pack:choose", {
          params: { path: { appearance_version_id: versionId } },
          body: { asset_id: assetId },
        }),
      ),
    onSuccess: (data) => job.setJobId(data.job_id),
  });
  const review = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/appearance-versions/{appearance_version_id}/identity-pack:review", {
          params: { path: { appearance_version_id: versionId } },
          body: {
            approve: Object.entries(decisions)
              .filter(([, d]) => d === "approved")
              .map(([id]) => id),
            reject: Object.entries(decisions)
              .filter(([, d]) => d === "rejected")
              .map(([id]) => id),
          },
        }),
      ),
    onSuccess: () => {
      setDecisions({});
      void client.invalidateQueries({ queryKey: packKey });
    },
  });
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/appearance-versions/{appearance_version_id}:approve", {
          params: { path: { appearance_version_id: versionId } },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: packKey }),
  });
  const newDraft = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/appearances/{appearance_id}/versions", {
          params: { path: { appearance_id: appearanceId } },
          body: {},
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["appearances"] }),
  });
  if (!pack.data) return <Skeleton className="h-48" />;
  const data = pack.data.identity_pack as { candidates?: { asset_id: string; mock?: boolean }[]; images?: PackImage[] };
  const checks = pack.data.age_checks as Json;
  const draft = pack.data.status === "draft";
  const { angles, expressions } = groupPack(data.images ?? []);
  const gallery = (images: PackImage[]) => (
    <div className="flex flex-wrap gap-3">
      {images.map((image) => (
        <figure key={image.asset_id} className="flex w-32 flex-col gap-1">
          <AssetImage assetId={image.asset_id} alt={`${image.kind} ${image.label}`} />
          <figcaption className="text-xs">
            {humanize(image.label)} · {image.similarity === null ? "no score" : image.similarity.toFixed(3)}{" "}
            <MockBadge mock={image.mock} />
            <div>{humanize(decisions[image.asset_id] ?? image.decision)}</div>
          </figcaption>
          {draft ? (
            <span className="flex gap-1">
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDecisions({ ...decisions, [image.asset_id]: "approved" })}
              >
                ✓
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={() => setDecisions({ ...decisions, [image.asset_id]: "rejected" })}
              >
                ✗
              </Button>
            </span>
          ) : null}
        </figure>
      ))}
    </div>
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {name} — {humanize(pack.data.status)}
        </CardTitle>
        <CardDescription>
          Candidates → canonical face → angles and expressions conditioned on it, scored against it (face.embed). The
          VLM apparent-age estimate is one of three age checks (§17.3).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <JobLine status={job.status} job={job.job} />
        <ErrorNote error={generate.error ?? choose.error ?? review.error ?? approve.error ?? newDraft.error} />
        {draft ? (
          <div className="flex gap-2">
            <Button onClick={() => generate.mutate()} disabled={generate.isPending}>
              Generate face candidates
            </Button>
          </div>
        ) : (
          <div>
            <Button variant="outline" onClick={() => newDraft.mutate()}>
              New draft version
            </Button>
          </div>
        )}
        {data.candidates?.length ? (
          <section>
            <h3 className="mb-2 text-sm font-medium">Candidates (choose the canonical face)</h3>
            <div className="flex flex-wrap gap-3">
              {data.candidates.map((c) => (
                <button
                  key={c.asset_id}
                  type="button"
                  disabled={!draft}
                  className={`rounded border-2 ${pack.data.canonical_face_asset_id === c.asset_id ? "border-blue-700" : "border-transparent"}`}
                  onClick={() => choose.mutate(c.asset_id)}
                  aria-label="Choose as canonical face"
                >
                  <AssetImage assetId={c.asset_id} alt="face candidate" />
                </button>
              ))}
            </div>
          </section>
        ) : null}
        {angles.length ? (
          <section>
            <h3 className="mb-2 text-sm font-medium">Angles</h3>
            {gallery(angles)}
          </section>
        ) : null}
        {expressions.length ? (
          <section>
            <h3 className="mb-2 text-sm font-medium">Expressions</h3>
            {gallery(expressions)}
          </section>
        ) : null}
        {checks.vlm_estimate !== undefined ? (
          <Alert
            tone={
              checks.vlm_estimate !== null && Number(checks.vlm_estimate) < Number(checks.threshold ?? 25)
                ? "warning"
                : "info"
            }
          >
            {ageCheckText(checks)}
          </Alert>
        ) : null}
        {draft ? (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => review.mutate()} disabled={!Object.keys(decisions).length}>
              Save review
            </Button>
            <Button onClick={() => approve.mutate()} disabled={approve.isPending}>
              Approve appearance
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

// ====================================================================== Voice: design, test bench, lexicon
export function VoicePanel({ creatorId }: { creatorId: string }) {
  const client = useQueryClient();
  const voices = useVoices(creatorId);
  const design = useStudioJob([keys.voices(creatorId), ["voice-candidates"]]);
  const [form, setForm] = useState({ name: "", description: "", language: "en-US" });
  const start = useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/voices", { body: { ...form, kind: "designed", creator_id: creatorId, count: 3 } })),
    onSuccess: (data) => {
      design.setJobId(data.job_id);
      void client.invalidateQueries({ queryKey: keys.voices(creatorId) });
    },
  });
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Design a voice</CardTitle>
          <CardDescription>
            Candidates from a description. Cloning a real voice needs consent and stays off until V1.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="vname">Name</Label>
            <Input id="vname" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="vdesc">Description</Label>
            <Input
              id="vdesc"
              className="w-96"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="vlang">Language</Label>
            <Input
              id="vlang"
              className="w-28"
              value={form.language}
              onChange={(e) => setForm({ ...form, language: e.target.value })}
            />
          </div>
          <Button onClick={() => start.mutate()} disabled={!form.name || !form.description || start.isPending}>
            Design
          </Button>
          <ErrorNote error={start.error} />
        </CardContent>
        <CardContent>
          <JobLine status={design.status} job={design.job} />
        </CardContent>
      </Card>
      {(voices.data ?? []).map((voice) => (
        <VoiceCard key={voice.id} voiceId={voice.id} name={voice.name} />
      ))}
    </div>
  );
}

function VoiceCard({ voiceId, name }: { voiceId: string; name: string }) {
  const client = useQueryClient();
  const voice = useQuery({
    queryKey: ["voice", voiceId],
    queryFn: () => unwrap(api.GET("/v1/voices/{voice_id}", { params: { path: { voice_id: voiceId } } })),
  });
  const candidates = useQuery({
    queryKey: ["voice-candidates", voiceId],
    queryFn: () => unwrap(api.GET("/v1/voices/{voice_id}/candidates", { params: { path: { voice_id: voiceId } } })),
  });
  const select = useMutation({
    mutationFn: (candidateId: string) =>
      unwrap(
        api.POST("/v1/voices/{voice_id}/candidates/{candidate_id}:select", {
          params: { path: { voice_id: voiceId, candidate_id: candidateId } },
        }),
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["voice", voiceId] });
      await client.invalidateQueries({ queryKey: ["voice-candidates", voiceId] });
    },
  });
  const versions = voice.data?.versions ?? [];
  const latest = versions[versions.length - 1];
  return (
    <Card>
      <CardHeader>
        <CardTitle>{name}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <ErrorNote error={select.error} />
        {candidates.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Candidate</Th>
                <Th>Engine</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {candidates.data.map((c) => (
                <tr key={c.id}>
                  <Td>
                    {c.asset_id ? <AssetAudio assetId={c.asset_id} label="voice candidate" /> : "audio unavailable"}
                  </Td>
                  <Td className="text-xs">
                    {c.engine}
                    {c.selected ? " · selected" : ""}
                  </Td>
                  <Td>
                    <Button size="sm" variant="outline" onClick={() => select.mutate(c.id)} disabled={select.isPending}>
                      Select
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty>No candidates yet.</Empty>
        )}
        {latest ? (
          <VoiceVersionBench versionId={latest.id} current={latest.id === voice.data?.current_version_id} />
        ) : null}
      </CardContent>
    </Card>
  );
}

function VoiceVersionBench({ versionId, current = false }: { versionId: string; current?: boolean }) {
  const client = useQueryClient();
  const version = useQuery({
    queryKey: ["voice-version", versionId],
    queryFn: () =>
      unwrap(
        api.GET("/v1/voice-versions/{voice_version_id}/detail", { params: { path: { voice_version_id: versionId } } }),
      ),
  });
  const bench = useStudioJob([["voice-version", versionId]]);
  const [text, setText] = useState("Okay, here is the thing nobody tells you about this.");
  const [term, setTerm] = useState({ term: "", respelling: "" });
  const test = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/voice-versions/{voice_version_id}:test", {
          params: { path: { voice_version_id: versionId } },
          body: { text },
        }),
      ),
    onSuccess: (data) => bench.setJobId(data.job_id),
  });
  const patch = useMutation({
    mutationFn: (lexicon: { term: string; respelling: string }[]) =>
      unwrap(
        api.PATCH("/v1/voice-versions/{voice_version_id}", {
          params: { path: { voice_version_id: versionId } },
          body: { lexicon },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["voice-version", versionId] }),
  });
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/voice-versions/{voice_version_id}:approve", {
          params: { path: { voice_version_id: versionId } },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["voice-version", versionId] }),
  });
  if (!version.data) return <Skeleton className="h-24" />;
  const draft = version.data.status === "draft";
  const lexicon = (version.data.lexicon ?? []) as { term: string; respelling: string }[];
  const result = (bench.job?.result ?? null) as Json | null;
  return (
    <section className="flex flex-col gap-2 rounded border border-slate-200 p-3">
      <h3 className="text-sm font-medium">
        Version {version.data.number} · {humanize(version.data.status)}
        {current
          ? " · the voice's current version"
          : version.data.status === "approved"
            ? " · not the voice's current version"
            : ""}
        {Object.keys(version.data.wpm ?? {}).length ? ` · WPM ${JSON.stringify(version.data.wpm)}` : ""}
      </h3>
      <div className="flex items-end gap-2">
        <div className="flex flex-1 flex-col gap-1">
          <Label htmlFor={`bench-${versionId}`}>Test bench text (tags like [laughs] allowed)</Label>
          <Input id={`bench-${versionId}`} value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <Button onClick={() => test.mutate()} disabled={test.isPending}>
          Synthesize and measure
        </Button>
      </div>
      <JobLine status={bench.status} job={bench.job} />
      {result ? (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          {result.audio_artifact_id ? (
            <ArtifactAudio artifactId={String(result.audio_artifact_id)} label="test bench audio" />
          ) : null}
          <span>WER {String(result.wer)}</span>
          <span>WPM {String(result.wpm)}</span>
          <span>speech quality {String(result.speech_quality ?? "—")}</span>
          <span>speaker similarity {String(result.speaker_similarity ?? "not measured")}</span>
          <MockBadge mock={Boolean(result.mock)} />
        </div>
      ) : null}
      <div>
        <h4 className="text-xs font-medium uppercase text-slate-600">Lexicon</h4>
        <ul className="text-sm">
          {lexicon.map((e) => (
            <li key={e.term}>
              {e.term} → {e.respelling}
            </li>
          ))}
        </ul>
        {draft ? (
          <div className="flex items-end gap-2">
            <Input
              aria-label="Term"
              placeholder="term"
              value={term.term}
              onChange={(e) => setTerm({ ...term, term: e.target.value })}
              className="w-40"
            />
            <Input
              aria-label="Respelling"
              placeholder="respelling"
              value={term.respelling}
              onChange={(e) => setTerm({ ...term, respelling: e.target.value })}
              className="w-40"
            />
            <Button
              size="sm"
              variant="outline"
              disabled={!term.term || !term.respelling}
              onClick={() => {
                patch.mutate([...lexicon, term]);
                setTerm({ term: "", respelling: "" });
              }}
            >
              Add
            </Button>
            <Button size="sm" onClick={() => approve.mutate()}>
              Approve version
            </Button>
          </div>
        ) : null}
      </div>
      <ErrorNote error={test.error ?? patch.error ?? approve.error} />
    </section>
  );
}

// ====================================================================== Wardrobe
export function WardrobePanel({ creatorId }: { creatorId: string }) {
  const client = useQueryClient();
  const wardrobes = useQuery({
    queryKey: ["wardrobes", creatorId],
    queryFn: () =>
      unwrap(api.GET("/v1/creators/{creator_id}/wardrobes", { params: { path: { creator_id: creatorId } } })),
  });
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creators/{creator_id}/wardrobes", {
          params: { path: { creator_id: creatorId } },
          body: { name, spec: { name } as never },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["wardrobes", creatorId] }),
  });
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardContent className="flex items-end gap-2 pt-4">
          <div className="flex flex-col gap-1">
            <Label htmlFor="wname">New outfit</Label>
            <Input id="wname" value={name} onChange={(e) => setName(e.target.value)} placeholder="grey hoodie" />
          </div>
          <Button onClick={() => create.mutate()} disabled={!name}>
            Create
          </Button>
          <ErrorNote error={create.error} />
        </CardContent>
      </Card>
      {(wardrobes.data ?? []).map((w) => {
        const latest = w.versions[w.versions.length - 1];
        return latest ? <WardrobeVersionCard key={w.id} name={w.name} versionId={latest.id} /> : null;
      })}
    </div>
  );
}

function WardrobeVersionCard({ name, versionId }: { name: string; versionId: string }) {
  const client = useQueryClient();
  const key = ["wardrobe-version", versionId];
  const version = useQuery({
    queryKey: key,
    queryFn: () =>
      unwrap(
        api.GET("/v1/wardrobe-versions/{wardrobe_version_id}", {
          params: { path: { wardrobe_version_id: versionId } },
        }),
      ),
  });
  const job = useStudioJob([key]);
  const generate = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/wardrobe-versions/{wardrobe_version_id}/references:generate", {
          params: { path: { wardrobe_version_id: versionId } },
        }),
      ),
    onSuccess: (data) => job.setJobId(data.job_id),
  });
  const approve = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/wardrobe-versions/{wardrobe_version_id}:approve", {
          params: { path: { wardrobe_version_id: versionId } },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: key }),
  });
  if (!version.data) return <Skeleton className="h-24" />;
  const draft = version.data.status === "draft";
  const scores = ((job.job?.result as Json | undefined)?.references ?? []) as {
    asset_id: string;
    similarity: number | null;
    view: string;
  }[];
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {name} · version {version.data.number} · {humanize(version.data.status)}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <div className="flex flex-wrap gap-3">
          {version.data.reference_asset_ids.map((id) => (
            <figure key={id} className="w-32">
              <AssetImage assetId={id} alt="wardrobe reference" />
              <figcaption className="text-xs">
                {(() => {
                  const s = scores.find((r) => r.asset_id === id);
                  return s ? `${humanize(s.view)} · ${s.similarity?.toFixed(3) ?? "no score"}` : "";
                })()}
              </figcaption>
            </figure>
          ))}
        </div>
        {draft ? (
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => generate.mutate()}>
              Generate references
            </Button>
            <Button onClick={() => approve.mutate()}>Approve</Button>
          </div>
        ) : null}
        <JobLine status={job.status} job={job.job} />
        <ErrorNote error={generate.error ?? approve.error} />
      </CardContent>
    </Card>
  );
}

// ====================================================================== Creator Test
export function CreatorTestPanel({ creator }: { creator: Creator }) {
  const client = useQueryClient();
  const historyKey = ["creator-tests", creator.id];
  const history = useQuery({
    queryKey: historyKey,
    queryFn: () => unwrap(api.GET("/v1/creators/{creator_id}/tests", { params: { path: { creator_id: creator.id } } })),
  });
  const run = useStudioJob([historyKey, ["baselines", creator.id]]);
  const [versionId, setVersionId] = useState(editableDraft(creator.versions)?.id ?? creator.current_version_id ?? "");
  const [selected, setSelected] = useState<string | null>(null);
  const start = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creator-versions/{creator_version_id}/tests", {
          params: { path: { creator_version_id: versionId } },
          body: { language: "en-US" },
        }),
      ),
    onSuccess: (data) => {
      run.setJobId(data.job_id);
      setSelected(data.creator_test_id);
      void client.invalidateQueries({ queryKey: historyKey });
    },
  });
  const tests = history.data ?? [];
  const current = tests.find((t) => t.id === selected) ?? tests[0];
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Run the Creator Test</CardTitle>
          <CardDescription>
            A fixed ~20 s script (neutral → excited → hesitant → serious, a look-away, a small laugh, one emphasis) in
            the creator&apos;s default world; drafts can be tested.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="test-version">Version</Label>
            <Select id="test-version" value={versionId} onChange={(e) => setVersionId(e.target.value)}>
              {creator.versions.map((v) => (
                <option key={v.id} value={v.id}>
                  v{v.number} · {v.status}
                </option>
              ))}
            </Select>
          </div>
          <Button onClick={() => start.mutate()} disabled={!versionId || start.isPending}>
            Run test
          </Button>
        </CardContent>
        <CardContent>
          <JobLine status={run.status} job={run.job} />
          <ErrorNote error={start.error} />
        </CardContent>
      </Card>
      {current ? (
        <Scorecard test={current as never} onRated={() => client.invalidateQueries({ queryKey: historyKey })} />
      ) : (
        <Empty>No tests yet.</Empty>
      )}
      {tests.length > 1 ? (
        <Card>
          <CardHeader>
            <CardTitle>History</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="text-sm">
              {tests.map((t) => (
                <li key={t.id}>
                  <button type="button" className="text-blue-800 hover:underline" onClick={() => setSelected(t.id)}>
                    {when(t.created_at)} · {String((t.scorecard as Json).status ?? "")}
                  </button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

function Scorecard({
  test,
  onRated,
}: {
  test: { id: string; scorecard: Json; render_artifact_id: string | null; created_at: string };
  onRated: () => void;
}) {
  const [rating, setRating] = useState({ human_rating: 3, same_person_rating: 3, accent_rating: 3 });
  const rate = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/creator-tests/{creator_test_id}/ratings", {
          params: { path: { creator_test_id: test.id } },
          body: { ...rating, notes: "" },
        }),
      ),
    onSuccess: onRated,
  });
  const rows = scorecardRows(test.scorecard);
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Scorecard · {when(test.created_at)} · {String(test.scorecard.status ?? "")}
        </CardTitle>
        <CardDescription>
          {coverageSummary(test.scorecard)}. Mock values check the pipeline; they say nothing about the creator.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {rows.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Metric</Th>
                <Th>Value</Th>
                <Th>How</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.key}>
                  <Td>{r.label}</Td>
                  <Td className={r.measured ? "" : "text-slate-600"}>
                    {r.value} <MockBadge mock={r.mock} />
                  </Td>
                  <Td className="text-xs text-slate-600">{r.note}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Alert tone="info">The test is still running.</Alert>
        )}
        <div className="flex flex-wrap items-end gap-2">
          {(["human_rating", "same_person_rating", "accent_rating"] as const).map((field) => (
            <div key={field} className="flex flex-col gap-1">
              <Label htmlFor={`${test.id}-${field}`}>{humanize(field)}</Label>
              <Select
                id={`${test.id}-${field}`}
                value={rating[field]}
                onChange={(e) => setRating({ ...rating, [field]: Number(e.target.value) })}
              >
                {[1, 2, 3, 4, 5].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </Select>
            </div>
          ))}
          <Button variant="outline" onClick={() => rate.mutate()}>
            Save ratings
          </Button>
        </div>
        <ErrorNote error={rate.error} />
      </CardContent>
    </Card>
  );
}

// ====================================================================== Consent
export function ConsentPanel() {
  const consents = useQuery({ queryKey: ["consents"], queryFn: () => unwrap(api.GET("/v1/consents")) });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Consent</CardTitle>
        <CardDescription>
          Digital twins and voice cloning need a verified consent; those flows are disabled until V1
          (digital_twins_enabled=false). This creator is synthetic.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {consents.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Subject</Th>
                <Th>Scope</Th>
                <Th>Status</Th>
                <Th>Expires</Th>
              </tr>
            </thead>
            <tbody>
              {consents.data.map((c) => (
                <tr key={c.id}>
                  <Td>{c.subject_name}</Td>
                  <Td>{c.scope}</Td>
                  <Td>
                    <Badge variant={c.valid ? "success" : "muted"}>{humanize(c.status)}</Badge>
                  </Td>
                  <Td className="text-xs">{when(c.expires_at)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty>No consents recorded.</Empty>
        )}
      </CardContent>
    </Card>
  );
}
