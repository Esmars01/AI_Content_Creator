"use client";
import { use, useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { ContinuityPanel, PlatesPanel, WorldDnaPanel, WorldVersionsPanel } from "@/components/studio/world-panels";
import { Empty, Skeleton, Tabs } from "@/components/ui/misc";
import { humanize } from "@/lib/format";
import { useWorld } from "@/lib/queries";

const TABS = [
  { id: "dna", label: "World DNA" },
  { id: "plates", label: "Plates" },
  { id: "versions", label: "Versions" },
  { id: "continuity", label: "Continuity" },
] as const;

export default function WorldPage({ params }: { params: Promise<{ worldId: string }> }) {
  const { worldId } = use(params);
  const [tab, setTab] = useState<(typeof TABS)[number]["id"]>("dna");
  const world = useWorld(worldId);
  if (world.isLoading) return <Skeleton className="h-64" />;
  if (!world.data) return <Empty>This world does not exist.</Empty>;
  return (
    <>
      <PageHeader
        title={world.data.name}
        description={`${humanize(world.data.kind)} · ${world.data.versions.length} version(s) · World Studio`}
      />
      <div className="mb-4">
        <Tabs tabs={TABS} value={tab} onChange={setTab} label="World Studio" />
      </div>
      {tab === "dna" ? <WorldDnaPanel world={world.data} /> : null}
      {tab === "plates" ? <PlatesPanel world={world.data} /> : null}
      {tab === "versions" ? <WorldVersionsPanel world={world.data} /> : null}
      {tab === "continuity" ? <ContinuityPanel worldId={worldId} /> : null}
    </>
  );
}
