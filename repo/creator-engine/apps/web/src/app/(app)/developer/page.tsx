"use client";
/** Developer basics (§31): raw spec, CBS, plan report and BuildManifest viewers for one version. */
import { useQuery } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState, type FormEvent } from "react";

import { PageHeader } from "@/components/app-shell";
import { JsonViewer } from "@/components/json-viewer";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import { Alert, Skeleton, Tabs } from "@/components/ui/misc";
import { api, ApiError, unwrap } from "@/lib/api";
import { useBehavior, usePreviz, useVersion } from "@/lib/queries";

type Tab = "spec" | "cbs" | "report" | "manifest";
const TABS = [
  { id: "spec", label: "VideoSpec" },
  { id: "cbs", label: "CBS" },
  { id: "report", label: "Plan report" },
  { id: "manifest", label: "BuildManifest" },
] as const;

function Viewers({ versionId }: { versionId: string }) {
  const [tab, setTab] = useState<Tab>("spec");
  const version = useVersion(versionId);
  const scenes = ((version.data?.spec as { scenes?: { key: string }[] } | undefined)?.scenes ?? []).map((s) => s.key);
  const [scene, setScene] = useState<string | null>(null);
  const behavior = useBehavior(tab === "cbs" ? versionId : null, scene ?? scenes[0] ?? null);
  const previz = usePreviz(tab === "report" ? versionId : null);
  const manifest = useQuery({
    queryKey: ["version", versionId, "manifest"],
    enabled: tab === "manifest",
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/manifest", { params: { path: { version_id: versionId } } })),
    retry: false,
  });
  if (version.error) return <Alert tone="danger">{version.error.message}</Alert>;
  if (!version.data) return <Skeleton className="h-64" />;
  const notYet = (error: unknown, what: string) =>
    error instanceof ApiError && error.status === 404 ? <Alert tone="info">{what}</Alert> : null;
  return (
    <div className="flex flex-col gap-3">
      <Tabs tabs={TABS} value={tab} onChange={setTab} label="Developer viewers" />
      {tab === "spec" ? <JsonViewer value={version.data.spec} label="VideoSpec (API form)" /> : null}
      {tab === "cbs" ? (
        <>
          <div className="flex items-center gap-2">
            <Label htmlFor="scene">Scene</Label>
            <Select
              id="scene"
              className="w-48"
              value={scene ?? scenes[0] ?? ""}
              onChange={(e) => setScene(e.target.value)}
            >
              {scenes.map((key) => (
                <option key={key} value={key}>
                  {key}
                </option>
              ))}
            </Select>
          </div>
          {notYet(behavior.error, "No CBS yet: it is resolved when the version is generated.")}
          {behavior.data ? <JsonViewer value={behavior.data.cbs} label="Canonical Behavior Spec" /> : null}
          {behavior.data ? <JsonViewer value={behavior.data.compiled} label="Compiled behavior" /> : null}
        </>
      ) : null}
      {tab === "report" ? (
        previz.data ? (
          <JsonViewer value={previz.data.plan_report} label="Plan report" />
        ) : (
          <Skeleton className="h-24" />
        )
      ) : null}
      {tab === "manifest" ? (
        <>
          {manifest.data ? <JsonViewer value={manifest.data} label="BuildManifest" /> : null}
          {manifest.isLoading ? <Skeleton className="h-24" /> : null}
        </>
      ) : null}
    </div>
  );
}

function Developer() {
  const params = useSearchParams();
  const router = useRouter();
  const versionId = params.get("version");
  const [value, setValue] = useState(versionId ?? "");
  function submit(event: FormEvent) {
    event.preventDefault();
    if (value.trim()) router.replace(`/developer?version=${encodeURIComponent(value.trim())}`);
  }
  return (
    <>
      <form className="mb-4 flex items-end gap-2" onSubmit={submit}>
        <div className="flex w-full max-w-[28rem] flex-col gap-1">
          <Label htmlFor="version-id">Version id</Label>
          <Input id="version-id" value={value} onChange={(e) => setValue(e.target.value)} placeholder="01a1…" />
        </div>
        <Button type="submit">Open</Button>
      </form>
      {versionId ? <Viewers versionId={versionId} /> : null}
    </>
  );
}

export default function DeveloperPage() {
  return (
    <>
      <PageHeader
        title="Developer"
        description="Raw documents of a version. The live queue, node inspector and cost ledger arrive with later phases."
      />
      <Suspense fallback={<Skeleton className="h-24" />}>
        <Developer />
      </Suspense>
    </>
  );
}
