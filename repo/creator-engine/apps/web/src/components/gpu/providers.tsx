"use client";
/** GPU providers for platform admins (cutover §6–§8, §22): create and edit provider rows, test one
 * (its health call and a read-only offer search: nothing rented), approve paid spending with a note
 * (audited), disable or delete, and see why the scheduler skipped one. Credentials are references
 * (`env:NAME`, `file:/path`); this page never asks for, shows or stores a key. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { ConfirmAction } from "@/components/gpu/confirm";
import { errorText } from "@/components/gpu/workers";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, type Schemas, unwrap } from "@/lib/api";
import { usd } from "@/lib/format";

type Provider = Schemas["ProviderOut"];
type TestResult = Schemas["ProviderTestOut"];

const REF_HINT = "env:VAST_API_KEY or file:/run/secrets/vast_api_key — a reference, never the key itself";

function parseConfig(text: string): Record<string, unknown> {
  if (!text.trim()) return {};
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("config must be a JSON object");
  return value as Record<string, unknown>;
}

function ProviderForm({
  registered,
  existing,
  onDone,
}: {
  registered: Schemas["RegisteredProviderOut"][];
  existing?: Provider;
  onDone: () => void;
}) {
  const client = useQueryClient();
  const [kind, setKind] = useState(existing?.kind ?? "");
  const [name, setName] = useState(existing?.name ?? "");
  const [ref, setRef] = useState(existing?.credentials_ref ?? "");
  const [regions, setRegions] = useState((existing?.regions ?? []).join(", "));
  const [budget, setBudget] = useState(existing?.budget_daily_usd != null ? String(existing.budget_daily_usd) : "");
  const [config, setConfig] = useState(() => {
    const rest = { ...((existing?.config ?? {}) as Record<string, unknown>) };
    delete rest.allow_paid; // approved separately, with a note
    return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : "";
  });
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name,
        credentials_ref: ref.trim() || null,
        regions: regions
          .split(",")
          .map((r) => r.trim())
          .filter(Boolean),
        budget_daily_usd: budget.trim() ? Number(budget) : null,
        config: { ...parseConfig(config), ...(existing?.config?.allow_paid ? { allow_paid: true } : {}) },
      };
      if (existing)
        return unwrap(
          api.PATCH("/v1/admin/gpu/providers/{provider_id}", {
            params: { path: { provider_id: existing.id } },
            body: body as never,
          }),
        );
      return unwrap(api.POST("/v1/admin/gpu/providers", { body: { ...body, kind, enabled: false } as never }));
    },
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["gpu"] });
      onDone();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate();
  }
  return (
    <form className="flex flex-col gap-2 rounded border border-slate-200 p-3" onSubmit={submit}>
      <div className="flex flex-wrap gap-2">
        {existing ? null : (
          <div className="flex flex-col gap-1">
            <Label htmlFor="p-kind">Kind</Label>
            <Select id="p-kind" value={kind} onChange={(e) => setKind(e.target.value)} required>
              <option value="">Choose a provider plugin</option>
              {registered.map((r) => (
                <option key={r.key} value={r.key}>
                  {r.name} ({r.key}){r.paid ? " — paid" : ""}
                </option>
              ))}
            </Select>
          </div>
        )}
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-name">Name</Label>
          <Input id="p-name" value={name} onChange={(e) => setName(e.target.value)} required />
        </div>
        <div className="flex min-w-72 flex-col gap-1">
          <Label htmlFor="p-ref">Credentials reference</Label>
          <Input id="p-ref" value={ref} placeholder="env:VAST_API_KEY" onChange={(e) => setRef(e.target.value)} />
          <span className="text-xs text-slate-600">{REF_HINT}</span>
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-regions">Regions</Label>
          <Input id="p-regions" value={regions} placeholder="eu, us" onChange={(e) => setRegions(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="p-budget">Daily budget (USD)</Label>
          <Input
            id="p-budget"
            type="number"
            min="0"
            step="0.5"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
          />
        </div>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="p-config">Settings (JSON, no secrets)</Label>
        <Textarea
          id="p-config"
          rows={5}
          className="font-mono text-xs"
          value={config}
          placeholder='{"max_price_per_hour_usd": 1.8, "image_template": "ghcr.io/<org>/creator-engine-worker-{family}:{variant}"}'
          onChange={(e) => setConfig(e.target.value)}
        />
        <span className="text-xs text-slate-600">
          Overrides of the plugin&apos;s defaults (price ceiling, image, classes, storage). Paid spending is approved
          separately.
        </span>
      </div>
      <div className="flex gap-2">
        <Button type="submit" disabled={save.isPending || (!existing && !kind) || !name}>
          {existing ? "Save" : "Create (disabled)"}
        </Button>
        <Button type="button" variant="outline" onClick={onDone}>
          Cancel
        </Button>
      </div>
      {save.error ? <Alert tone="danger">{errorText(save.error)}</Alert> : null}
    </form>
  );
}

function PaidApproval({ provider }: { provider: Provider }) {
  const client = useQueryClient();
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);
  const change = useMutation({
    mutationFn: (allow: boolean) =>
      unwrap(
        api.PATCH("/v1/admin/gpu/providers/{provider_id}", {
          params: { path: { provider_id: provider.id } },
          body: { config: { ...(provider.config ?? {}), allow_paid: allow }, note } as never,
        }),
      ),
    onSuccess: () => {
      setOpen(false);
      setNote("");
      return client.invalidateQueries({ queryKey: ["gpu"] });
    },
  });
  if (!provider.paid) return <span className="text-xs">free</span>;
  if (provider.paid_approved)
    return (
      <div className="flex flex-col gap-1">
        <Badge>spending approved</Badge>
        <Button size="sm" variant="outline" disabled={change.isPending} onClick={() => change.mutate(false)}>
          Revoke approval
        </Button>
        {change.error ? <Alert tone="danger">{errorText(change.error)}</Alert> : null}
      </div>
    );
  if (!open)
    return (
      <div className="flex flex-col gap-1">
        <Badge variant="outline">spending off</Badge>
        <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
          Approve spending…
        </Button>
      </div>
    );
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`note-${provider.id}`}>The owner&apos;s approval (audited)</Label>
      <Textarea
        id={`note-${provider.id}`}
        rows={2}
        value={note}
        placeholder="Owner approved up to 5 USD/day for the A100 smoke test"
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="flex gap-1">
        <Button size="sm" disabled={!note.trim() || change.isPending} onClick={() => change.mutate(true)}>
          Approve
        </Button>
        <Button size="sm" variant="outline" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
      {change.error ? <Alert tone="danger">{errorText(change.error)}</Alert> : null}
    </div>
  );
}

function ProviderRow({ provider, registered }: { provider: Provider; registered: Schemas["RegisteredProviderOut"][] }) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);
  const test = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/admin/gpu/providers/{provider_id}:test", { params: { path: { provider_id: provider.id } } }),
      ),
    onSuccess: setResult,
  });
  const toggle = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/v1/admin/gpu/providers/{provider_id}", {
          params: { path: { provider_id: provider.id } },
          body: { enabled: !provider.enabled } as never,
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  const remove = useMutation({
    mutationFn: () =>
      unwrap(
        api.DELETE("/v1/admin/gpu/providers/{provider_id}", {
          params: { path: { provider_id: provider.id } },
          body: { confirm: provider.name },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  const maxPrice = (provider.config as { max_price_per_hour_usd?: number } | undefined)?.max_price_per_hour_usd;
  return (
    <>
      <tr className="align-top">
        <Td>
          <div className="font-medium">{provider.name}</div>
          <div className="text-xs text-slate-600">{provider.kind}</div>
          <div className="font-mono text-xs text-slate-600">
            {provider.credentials_ref ?? "no credentials reference"}
          </div>
        </Td>
        <Td>
          {provider.enabled ? "enabled" : "disabled"}
          <div className="text-xs">
            {provider.loaded == null
              ? "scheduler unreachable"
              : provider.loaded
                ? provider.healthy
                  ? "loaded · healthy"
                  : `loaded · ${provider.health_detail ?? "unhealthy"}`
                : "not loaded"}
          </div>
        </Td>
        <Td>
          <PaidApproval provider={provider} />
        </Td>
        <Td className="text-xs">
          <div>Daily cap: {provider.budget_daily_usd != null ? usd(provider.budget_daily_usd) : "none"}</div>
          <div>Max price: {maxPrice != null ? `${usd(maxPrice)}/h` : "plugin default"}</div>
          <div>
            Today: {provider.spent_today_usd != null ? usd(provider.spent_today_usd) : "unknown"} spent,{" "}
            {provider.projected_today_usd != null ? usd(provider.projected_today_usd) : "unknown"} projected
          </div>
          <div>Regions: {provider.regions.join(", ") || "plugin default"}</div>
        </Td>
        <Td>
          <div className="flex flex-col items-start gap-1">
            <Button size="sm" variant="outline" disabled={test.isPending} onClick={() => test.mutate()}>
              {test.isPending ? "Testing…" : "Test"}
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(!editing)}>
              Edit
            </Button>
            <Button size="sm" variant="outline" disabled={toggle.isPending} onClick={() => toggle.mutate()}>
              {provider.enabled ? "Disable" : "Enable"}
            </Button>
            <ConfirmAction
              label="Delete"
              phrase={provider.name}
              warning="Deleting removes this provider row. A provider with workers or costs on record can only be disabled."
              disabled={remove.isPending}
              onConfirm={() => remove.mutate()}
            />
          </div>
        </Td>
      </tr>
      {test.error || toggle.error || remove.error || result ? (
        <tr>
          <Td colSpan={5}>
            {test.error || toggle.error || remove.error ? (
              <Alert tone="danger">{errorText(test.error ?? toggle.error ?? remove.error)}</Alert>
            ) : null}
            {result ? (
              <Alert tone={result.healthy ? (result.offers ? "success" : "warning") : "danger"}>
                {result.healthy ? "Reachable" : `Not healthy: ${result.detail}`}
                {result.healthy
                  ? ` · ${result.offers} live offers${result.cheapest_per_hour_usd != null ? `, from ${usd(result.cheapest_per_hour_usd)}/h` : ""}`
                  : ""}
                {result.classes?.length ? ` · classes ${result.classes.join(", ")}` : ""}
                {result.offers_error ? ` · offers failed: ${result.offers_error}` : ""} (read-only: nothing rented)
              </Alert>
            ) : null}
          </Td>
        </tr>
      ) : null}
      {editing ? (
        <tr>
          <Td colSpan={5}>
            <ProviderForm registered={registered} existing={provider} onDone={() => setEditing(false)} />
          </Td>
        </tr>
      ) : null}
    </>
  );
}

function Orphans() {
  const client = useQueryClient();
  const orphans = useQuery({
    queryKey: ["gpu", "orphans"],
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/orphans")),
    retry: false,
  });
  const terminate = useMutation({
    mutationFn: (o: Schemas["OrphanOut"]) =>
      unwrap(
        api.POST("/v1/admin/gpu/orphans:terminate", {
          body: { provider: o.provider, external_id: o.external_id, confirm: o.external_id },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  if (orphans.error) return <Alert tone="warning">Orphan check unavailable: {errorText(orphans.error)}</Alert>;
  if (!orphans.data?.length) return null;
  return (
    <Alert tone="danger">
      <div className="flex flex-col gap-2">
        <span>
          Instances labeled as this platform&apos;s that no worker tracks (they may still bill): {orphans.data.length}
        </span>
        {orphans.data.map((o) => (
          <div key={`${o.provider}-${o.external_id}`} className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-xs">
              {o.provider} {o.external_id} ({o.state}, {usd(o.price_per_hour_usd)}/h)
            </code>
            <ConfirmAction
              label="Terminate"
              phrase="terminate"
              warning="Destroys this instance at the provider."
              onConfirm={() => terminate.mutate(o)}
            />
          </div>
        ))}
        {terminate.error ? <span>{errorText(terminate.error)}</span> : null}
      </div>
    </Alert>
  );
}

export function Providers() {
  const [creating, setCreating] = useState(false);
  const providers = useQuery({
    queryKey: ["gpu", "providers"],
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/providers")),
  });
  const registered = providers.data?.registered ?? [];
  const skipped = Object.entries(providers.data?.skipped ?? {});
  return (
    <Card>
      <CardHeader>
        <CardTitle>Providers</CardTitle>
        <CardDescription>
          A paid provider is used only through an enabled row whose spending the owner approved (a noted change,
          audited), within its price ceiling and daily budget. Test makes read-only calls: nothing is rented.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {providers.isLoading ? <Skeleton className="h-16" /> : null}
        {providers.error ? <Alert tone="warning">{errorText(providers.error)}</Alert> : null}
        {skipped.map(([key, reason]) => (
          <Alert key={key} tone="warning">
            The scheduler could not configure {key}: {reason}
          </Alert>
        ))}
        <Orphans />
        {providers.data ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              Installed plugins:
              {registered.map((r) => (
                <Badge key={r.key} variant="outline">
                  {r.key}
                  {r.paid ? " · paid" : ""}
                  {r.mock ? " · simulated" : ""}
                </Badge>
              ))}
            </div>
            {providers.data.rows.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Provider</Th>
                    <Th>Status</Th>
                    <Th>Spending</Th>
                    <Th>Limits and spend</Th>
                    <Th>Actions</Th>
                  </tr>
                </thead>
                <tbody>
                  {providers.data.rows.map((p) => (
                    <ProviderRow key={p.id} provider={p} registered={registered} />
                  ))}
                </tbody>
              </Table>
            ) : (
              <Empty>No provider configured yet. Add one to rent GPU workers.</Empty>
            )}
            {creating ? (
              <ProviderForm registered={registered} onDone={() => setCreating(false)} />
            ) : (
              <div>
                <Button variant="outline" onClick={() => setCreating(true)}>
                  Add a provider
                </Button>
              </div>
            )}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}
