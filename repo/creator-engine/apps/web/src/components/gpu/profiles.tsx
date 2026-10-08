"use client";
/** Model profiles (cutover §9, §13–§14): what one instance serves, with the disk and VRAM computed
 * from the plugin manifests (declared sizes, labeled as such), and provisioning of a profile on a
 * configured provider. Each worker prepares its models right after boot; a paid provider needs the
 * explicit acknowledgement before anything is rented. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { errorText } from "@/components/gpu/workers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, type Schemas, unwrap } from "@/lib/api";
import { provisionChoices } from "@/lib/gpu";

type Profile = Schemas["ProfileOut"];

function ProfileCard({ profile, providers }: { profile: Profile; providers: Schemas["ProvidersOut"] | undefined }) {
  const client = useQueryClient();
  const choices = provisionChoices(providers);
  const [choice, setChoice] = useState("");
  const [region, setRegion] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const selected = choices.find((c) => c.value === choice);
  const sizing = profile.sizing;
  const run = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/admin/gpu/profiles/{profile_id}:provision", {
          params: { path: { profile_id: profile.id } },
          body: {
            ...(selected?.providerId
              ? { provider_id: selected.providerId }
              : { provider: choice.replace(/^key:/, "") }),
            region: region.trim() || null,
          },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  const ready = Boolean(selected && profile.enabled && sizing.fits && (!selected.paid || acknowledged));
  return (
    <div className="flex flex-col gap-2 rounded border border-slate-200 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{profile.label}</span>
        <code className="text-xs">{profile.id}</code>
        {profile.colocate ? (
          <Badge variant="outline">one instance</Badge>
        ) : (
          <Badge variant="outline">per component</Badge>
        )}
        {profile.enabled ? null : <Badge variant="outline">disabled</Badge>}
      </div>
      <div className="text-sm">
        {profile.gpu_class} ({profile.vram_gb} GB VRAM) · disk <strong>{sizing.disk_gb} GB</strong> = models{" "}
        {sizing.models_gb} GB + staging {sizing.staging_gb} GB + scratch {sizing.scratch_gb} GB + image{" "}
        {sizing.image_gb} GB (rounded up) · VRAM {sizing.vram_min_gb} GB minimum, {sizing.vram_recommended_gb} GB
        recommended {sizing.fits ? "" : "— does not fit the class"}
      </div>
      <Table>
        <thead>
          <tr>
            <Th>Model</Th>
            <Th>Adapter</Th>
            <Th>Declared size</Th>
            <Th>Source at revision</Th>
            <Th>License</Th>
          </tr>
        </thead>
        <tbody>
          {sizing.models.map((m) => (
            <tr key={m.key}>
              <Td className="font-mono text-xs">{m.key}</Td>
              <Td className="text-xs">{m.adapter}</Td>
              <Td>{m.declared_gb} GB (declared)</Td>
              <Td className="font-mono text-xs">
                {m.repo ?? "—"}@{m.revision ? m.revision.slice(0, 12) : "—"}
              </Td>
              <Td className="text-xs">{m.license ?? "—"}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      {sizing.notes?.map((note) => (
        <p key={note} className="text-xs text-slate-600">
          {note}
        </p>
      ))}
      {sizing.missing?.length ? <Alert tone="danger">Not installed: {sizing.missing.join(", ")}</Alert> : null}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor={`pf-provider-${profile.id}`}>Provider</Label>
          <Select
            id={`pf-provider-${profile.id}`}
            value={choice}
            onChange={(e) => {
              setChoice(e.target.value);
              setAcknowledged(false);
            }}
          >
            <option value="">Choose a provider</option>
            {choices.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
                {c.paid ? " — paid" : ""}
              </option>
            ))}
          </Select>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor={`pf-region-${profile.id}`}>Region</Label>
          <Input
            id={`pf-region-${profile.id}`}
            className="w-28"
            value={region}
            placeholder="eu"
            onChange={(e) => setRegion(e.target.value)}
          />
        </div>
        <Button disabled={!ready || run.isPending} onClick={() => run.mutate()}>
          {run.isPending ? "Provisioning…" : "Provision this profile"}
        </Button>
      </div>
      {selected?.paid ? (
        <label
          className="flex items-start gap-2 rounded border border-amber-300 bg-amber-50 p-2 text-sm"
          htmlFor={`pf-ack-${profile.id}`}
        >
          <input
            id={`pf-ack-${profile.id}`}
            type="checkbox"
            className="mt-1"
            checked={acknowledged}
            onChange={(e) => setAcknowledged(e.target.checked)}
          />
          <span>
            This rents a paid {profile.gpu_class} with a {sizing.disk_gb} GB disk; billing starts now and continues
            until it is stopped or terminated. The first boot downloads about {sizing.models_gb} GB of weights.
          </span>
        </label>
      ) : null}
      {run.error ? <Alert tone="danger">{errorText(run.error)}</Alert> : null}
      {run.data ? (
        <Alert tone={run.data.provisioned.length ? "success" : "warning"}>
          {run.data.provisioned.length
            ? `Provisioned ${run.data.provisioned.map((p) => String(p.external_id)).join(", ")}: the workers register, then prepare their models (see Workers).`
            : "Nothing provisioned"}
          {run.data.attempts.length ? ` — tried: ${run.data.attempts.join("; ")}` : ""}
        </Alert>
      ) : null}
    </div>
  );
}

export function Profiles() {
  const profiles = useQuery({
    queryKey: ["gpu", "profiles"],
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/profiles")),
    retry: false,
  });
  const providers = useQuery({
    queryKey: ["gpu", "providers"],
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/providers")),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Model profiles</CardTitle>
        <CardDescription>
          What one GPU instance serves (config/gpu/profiles.yaml). The disk is computed from the models&apos; declared
          sizes, not a fixed default; the workers measure the real bytes as they download.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {profiles.isLoading ? <Skeleton className="h-24" /> : null}
        {profiles.error ? <Alert tone="warning">{errorText(profiles.error)}</Alert> : null}
        {profiles.data?.length ? (
          profiles.data.map((p) => <ProfileCard key={p.id} profile={p} providers={providers.data} />)
        ) : profiles.data ? (
          <Empty>No model profile is configured.</Empty>
        ) : null}
      </CardContent>
    </Card>
  );
}
