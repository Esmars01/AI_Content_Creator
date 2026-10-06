"use client";
import Link from "next/link";
import { use, useState } from "react";

import { PageHeader, useAdvanced } from "@/components/app-shell";
import { JsonViewer } from "@/components/json-viewer";
import { ConsistencyPanel } from "@/components/consistency-panel";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  AppearancePanel,
  ConsentPanel,
  CreatorTestPanel,
  DnaEditor,
  MemoryPanel,
  VoicePanel,
  WardrobePanel,
} from "@/components/studio/creator-panels";
import { Empty, Skeleton, Tabs } from "@/components/ui/misc";
import type { Domain } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useCreator, useMemory, useWorlds } from "@/lib/queries";

type DNA = Domain["CreatorDNA"];

function DnaSummary({ dna }: { dna: DNA }) {
  const identity = dna.identity as unknown as Record<string, unknown>;
  const canon = (identity.canon as { subject?: string; predicate?: string; object?: string }[] | undefined) ?? [];
  const personality = dna.personality as unknown as Record<string, unknown> | undefined;
  const traits = (personality?.traits as Record<string, unknown> | undefined) ?? {};
  return (
    <dl className="grid grid-cols-[10rem_1fr] gap-x-4 gap-y-2 text-sm">
      <dt className="text-slate-600">Display name</dt>
      <dd>{String(identity.display_name ?? "—")}</dd>
      <dt className="text-slate-600">Bio</dt>
      <dd>{String(identity.bio ?? identity.short_bio ?? "—")}</dd>
      <dt className="text-slate-600">Canon</dt>
      <dd>
        {canon.length ? (
          <ul className="list-disc pl-5">
            {canon.map((fact, i) => (
              <li key={i}>
                {fact.subject} {humanize(fact.predicate ?? "").toLowerCase()} {fact.object}
              </li>
            ))}
          </ul>
        ) : (
          "—"
        )}
      </dd>
      <dt className="text-slate-600">Traits</dt>
      <dd className="flex flex-wrap gap-1">
        {Object.entries(traits).map(([k, v]) => (
          <Badge key={k} variant="outline">
            {humanize(k)} {typeof v === "number" ? v.toFixed(2) : String(v)}
          </Badge>
        ))}
      </dd>
    </dl>
  );
}

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "dna", label: "Creator DNA" },
  { id: "memory", label: "Memory" },
  { id: "appearance", label: "Appearance" },
  { id: "voice", label: "Voice" },
  { id: "wardrobe", label: "Wardrobe" },
  { id: "test", label: "Creator Test" },
  { id: "consistency", label: "Consistency" },
  { id: "consent", label: "Consent" },
] as const;

export default function CreatorPage({ params }: { params: Promise<{ creatorId: string }> }) {
  const { creatorId } = use(params);
  const initial = typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("tab");
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>(
    TABS.some((t) => t.id === initial) ? (initial as (typeof TABS)[number]["id"]) : "overview",
  );
  const advanced = useAdvanced();
  const creator = useCreator(creatorId);
  const memory = useMemory(creatorId);
  const worlds = useWorlds();
  if (creator.isLoading) return <Skeleton className="h-64" />;
  if (!creator.data) return <Empty>This creator does not exist.</Empty>;
  const current = creator.data.current_version;
  const defaults = new Set(current?.default_world_ids ?? []);
  const items = memory.data?.items ?? [];
  return (
    <>
      <PageHeader
        title={creator.data.name}
        description={`${humanize(creator.data.kind)} · ${creator.data.versions.length} version(s)`}
      />
      <div className="mb-4">
        <Tabs tabs={TABS} value={tab} onChange={setTab} label="Creator Studio" />
      </div>
      {tab === "dna" ? <DnaEditor creator={creator.data} /> : null}
      {tab === "memory" ? <MemoryPanel creatorId={creatorId} /> : null}
      {tab === "appearance" ? <AppearancePanel creatorId={creatorId} /> : null}
      {tab === "voice" ? <VoicePanel creatorId={creatorId} /> : null}
      {tab === "wardrobe" ? <WardrobePanel creatorId={creatorId} /> : null}
      {tab === "test" ? <CreatorTestPanel creator={creator.data} /> : null}
      {tab === "consistency" ? <ConsistencyPanel creatorId={creatorId} /> : null}
      {tab === "consent" ? <ConsentPanel /> : null}
      {tab === "overview" ? (
        <div className="grid grid-cols-3 gap-4">
          <Card className="col-span-2">
            <CardHeader>
              <CardTitle>Creator DNA {current ? `(version ${current.number}, ${current.status})` : ""}</CardTitle>
            </CardHeader>
            <CardContent>
              {current ? (
                advanced ? (
                  <JsonViewer value={current.dna} label="Creator DNA" />
                ) : (
                  <DnaSummary dna={current.dna as unknown as DNA} />
                )
              ) : (
                <Empty>No approved version yet.</Empty>
              )}
            </CardContent>
          </Card>
          <div className="flex flex-col gap-4">
            <Card>
              <CardHeader>
                <CardTitle>Worlds</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="text-sm">
                  {(worlds.data?.items ?? [])
                    .filter((w) => defaults.has(w.id) || w.owner_creator_id === creatorId)
                    .map((w) => (
                      <li key={w.id}>
                        <Link className="text-blue-800 hover:underline" href={`/worlds/${w.id}`}>
                          {w.name}
                        </Link>
                        {defaults.has(w.id) ? (
                          <Badge variant="outline" className="ml-1">
                            default
                          </Badge>
                        ) : null}
                      </li>
                    ))}
                </ul>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>Creator Memory</CardTitle>
              </CardHeader>
              <CardContent>
                {items.length ? (
                  <ul className="flex flex-col gap-1 text-sm">
                    {items.slice(0, 20).map((item) => (
                      <li key={item.id}>
                        {item.text || humanize(item.kind)}{" "}
                        <span className="text-xs text-slate-600">
                          {humanize(item.status)}
                          {item.pinned ? " · pinned" : ""}
                          {item.conflict_state === "unresolved" ? " · conflict" : ""}
                        </span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <Empty>No memory items.</Empty>
                )}
                <p className="mt-2 text-xs text-slate-600">Pin, forget and resolve conflicts in the Memory tab.</p>
              </CardContent>
            </Card>
          </div>
        </div>
      ) : null}
    </>
  );
}
