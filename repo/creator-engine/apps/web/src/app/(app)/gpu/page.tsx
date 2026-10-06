"use client";
/** GPU (§31, Phase 9): pools, workers and offers for members; providers, provisioning, stopping,
 * enrollment and the live queue for platform admins. Paid providers stay off until the owner
 * approves spending (a noted PATCH); this page never enables them by itself. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { PageHeader } from "@/components/app-shell";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Alert, Empty, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, ApiError, unwrap } from "@/lib/api";
import { humanize, usd, when } from "@/lib/format";
import { coldStart, holdLabel, isFleetWorker, poolState, spendState } from "@/lib/gpu";
import { useMe } from "@/lib/queries";

const FAMILIES = ["cpu_model", "image", "wan", "tts", "asr", "audio", "lipsync", "vision", "post", "vllm"] as const;

const gpuKeys = {
  pools: ["gpu", "pools"] as const,
  workers: ["gpu", "workers"] as const,
  offers: ["gpu", "offers"] as const,
  providers: ["gpu", "providers"] as const,
  queue: ["gpu", "queue"] as const,
};

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.problem?.detail ?? error.message;
  return error instanceof Error ? error.message : String(error);
}

function Pools() {
  const pools = useQuery({ queryKey: gpuKeys.pools, queryFn: () => unwrap(api.GET("/v1/gpu/pools")), retry: false });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Pools</CardTitle>
        <CardDescription>
          Desired workers = queued GPU-seconds of the pool&apos;s families ÷ its target latency, within min and max.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {pools.error ? <Alert tone="warning">{errorText(pools.error)}</Alert> : null}
        {pools.isLoading ? <Skeleton className="h-24" /> : null}
        {pools.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Pool</Th>
                <Th>State</Th>
                <Th>Workers</Th>
                <Th>Backlog</Th>
                <Th>Classes</Th>
                <Th>Providers</Th>
              </tr>
            </thead>
            <tbody>
              {pools.data.map((pool) => (
                <tr key={pool.id}>
                  <Td className="font-medium">{pool.id}</Td>
                  <Td>
                    <Badge variant={pool.enabled ? "default" : "outline"}>{poolState(pool)}</Badge>
                  </Td>
                  <Td>
                    {pool.current} / {pool.max} (desired {pool.desired})
                  </Td>
                  <Td>{Math.round(pool.backlog_seconds)} s</Td>
                  <Td className="text-xs">{pool.gpu_classes.join(", ")}</Td>
                  <Td className="text-xs">
                    {pool.providers.map((p) => (
                      <span
                        key={p}
                        className={pool.providers_available.includes(p) ? "" : "text-slate-500 line-through"}
                      >
                        {p}{" "}
                      </span>
                    ))}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Workers({ admin }: { admin: boolean }) {
  const client = useQueryClient();
  const workers = useQuery({ queryKey: gpuKeys.workers, queryFn: () => unwrap(api.GET("/v1/gpu/workers")) });
  const stop = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/v1/admin/gpu/workers/{worker_id}:stop", {
          params: { path: { worker_id: id } },
          body: { action: "terminate" },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <CardDescription>Live workers. Prices are captured from the provider at provision time.</CardDescription>
      </CardHeader>
      <CardContent>
        {stop.error ? <Alert tone="danger">{errorText(stop.error)}</Alert> : null}
        {workers.isLoading ? <Skeleton className="h-24" /> : null}
        {workers.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Worker</Th>
                <Th>State</Th>
                <Th>Class</Th>
                <Th>Price</Th>
                <Th>Cold start</Th>
                <Th>Heartbeat</Th>
                {admin ? <Th /> : null}
              </tr>
            </thead>
            <tbody>
              {workers.data.map((w) => (
                <tr key={w.id}>
                  <Td>
                    <div>{w.external_id ?? w.id}</div>
                    <div className="text-xs text-slate-600">
                      {w.provider_kind ?? "static"} · {humanize(w.runtime_family)}
                      {w.variant ? `:${w.variant}` : ""} · {w.pool_id ?? "no pool"}
                    </div>
                  </Td>
                  <Td>
                    <StateBadge state={w.state} />
                  </Td>
                  <Td className="text-xs">{w.gpu_type}</Td>
                  <Td>{usd(w.price_per_hour_usd)}/h</Td>
                  <Td>{coldStart(w)}</Td>
                  <Td className="text-xs">{when(w.last_heartbeat_at)}</Td>
                  {admin ? (
                    <Td>
                      {isFleetWorker(w) ? (
                        <Button size="sm" variant="outline" disabled={stop.isPending} onClick={() => stop.mutate(w.id)}>
                          Stop
                        </Button>
                      ) : null}
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        ) : workers.data ? (
          <Empty>No live workers.</Empty>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Offers() {
  const offers = useQuery({ queryKey: gpuKeys.offers, queryFn: () => unwrap(api.GET("/v1/gpu/offers")), retry: false });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Offers</CardTitle>
        <CardDescription>What the configured providers offer now. Paid offers cost money when used.</CardDescription>
      </CardHeader>
      <CardContent>
        {offers.error ? <Alert tone="warning">{errorText(offers.error)}</Alert> : null}
        {offers.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Provider</Th>
                <Th>Class</Th>
                <Th>Region</Th>
                <Th>VRAM</Th>
                <Th>Price</Th>
                <Th>Available</Th>
              </tr>
            </thead>
            <tbody>
              {offers.data.map((o) => (
                <tr key={`${o.provider}-${o.gpu_class}-${o.region}`}>
                  <Td>
                    {o.provider} {o.paid ? <Badge variant="outline">paid</Badge> : null}
                  </Td>
                  <Td className="text-xs">{o.gpu_class}</Td>
                  <Td className="text-xs">{o.region}</Td>
                  <Td>{o.vram_gb} GB</Td>
                  <Td>{usd(o.price_per_hour_usd)}/h</Td>
                  <Td>{o.available}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : offers.data ? (
          <Empty>No offers.</Empty>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Queue() {
  const queue = useQuery({ queryKey: gpuKeys.queue, queryFn: () => unwrap(api.GET("/v1/admin/gpu/queue")) });
  const spend = spendState(queue.data?.spend);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Queue and spend</CardTitle>
        <CardDescription>Held tasks wait until their budget allows them; they never lease.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Alert tone={spend.tone}>{spend.label}</Alert>
        {spend.fraction > 0 ? <Progress value={spend.fraction} label="Daily budget used (projected)" /> : null}
        {queue.data ? (
          <div className="text-sm">
            {Object.entries(queue.data.by_state)
              .map(([s, n]) => `${n} ${s}`)
              .join(" · ") || "Queue empty"}
            {Object.keys(queue.data.held).length
              ? ` · held: ${Object.entries(queue.data.held)
                  .map(([r, n]) => `${n} (${holdLabel(r)})`)
                  .join(", ")}`
              : ""}
          </div>
        ) : (
          <Skeleton className="h-8" />
        )}
        {queue.data?.tasks.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Task</Th>
                <Th>State</Th>
                <Th>Priority</Th>
                <Th>Estimate</Th>
                <Th>Hold</Th>
              </tr>
            </thead>
            <tbody>
              {queue.data.tasks.slice(0, 50).map((t) => (
                <tr key={t.id}>
                  <Td>
                    <div>{t.adapter_id ?? t.model_key}</div>
                    <div className="text-xs text-slate-600">{t.capability}</div>
                  </Td>
                  <Td>
                    <StateBadge state={t.state} />
                  </Td>
                  <Td>{t.priority}</Td>
                  <Td>{Math.round(t.est_seconds)} s</Td>
                  <Td className="text-xs">{holdLabel(t.held_reason)}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Provision() {
  const client = useQueryClient();
  const [provider, setProvider] = useState("mock");
  const [gpuClass, setGpuClass] = useState("mock_gpu");
  const [family, setFamily] = useState<(typeof FAMILIES)[number]>("cpu_model");
  const run = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/admin/gpu/workers:provision", {
          body: { provider, gpu_class: gpuClass, runtime_family: family, count: 1 },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  const enroll = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/admin/gpu/workers:enroll", { body: { runtime_family: family } })),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    run.mutate();
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Provision or enroll</CardTitle>
        <CardDescription>
          Provision one worker now, or issue a one-time token for a self-managed host (WORKER_TOKEN).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form className="flex flex-wrap items-end gap-2" onSubmit={submit}>
          <div className="flex flex-col gap-1">
            <Label htmlFor="provider">Provider</Label>
            <Input id="provider" className="w-40" value={provider} onChange={(e) => setProvider(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="gpu-class">GPU class</Label>
            <Input id="gpu-class" className="w-48" value={gpuClass} onChange={(e) => setGpuClass(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="family">Runtime family</Label>
            <Select id="family" value={family} onChange={(e) => setFamily(e.target.value as (typeof FAMILIES)[number])}>
              {FAMILIES.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </Select>
          </div>
          <Button type="submit" disabled={run.isPending}>
            Provision
          </Button>
          <Button type="button" variant="outline" disabled={enroll.isPending} onClick={() => enroll.mutate()}>
            Issue enrollment token
          </Button>
        </form>
        {run.error ? <Alert tone="danger">{errorText(run.error)}</Alert> : null}
        {run.data ? (
          <Alert tone={run.data.provisioned.length ? "success" : "warning"}>
            {run.data.provisioned.length
              ? `Provisioned ${run.data.provisioned.map((p) => p.external_id).join(", ")}`
              : "Nothing provisioned"}
            {run.data.attempts.length ? ` — tried: ${run.data.attempts.join("; ")}` : ""}
          </Alert>
        ) : null}
        {enroll.error ? <Alert tone="danger">{errorText(enroll.error)}</Alert> : null}
        {enroll.data ? (
          <Alert tone="info">
            Shown once, valid until {when(enroll.data.expires_at)}:{" "}
            <code className="break-all font-mono text-xs">{enroll.data.token}</code>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Providers() {
  const providers = useQuery({
    queryKey: gpuKeys.providers,
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/providers")),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Providers</CardTitle>
        <CardDescription>
          Configured rows (credentials are references such as env:RUNPOD_API_KEY, never values) and the installed
          provider plugins. Paid providers need an enabled row and the owner&apos;s noted approval (allow_paid).
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {providers.isLoading ? <Skeleton className="h-16" /> : null}
        {providers.data ? (
          <>
            <div className="flex flex-wrap gap-2 text-sm">
              {providers.data.registered.map((r) => (
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
                    <Th>Name</Th>
                    <Th>Kind</Th>
                    <Th>Enabled</Th>
                    <Th>Paid spending</Th>
                    <Th>Daily cap</Th>
                    <Th>Loaded</Th>
                  </tr>
                </thead>
                <tbody>
                  {providers.data.rows.map((p) => (
                    <tr key={p.id}>
                      <Td>{p.name}</Td>
                      <Td className="text-xs">{p.kind}</Td>
                      <Td>{p.enabled ? "yes" : "no"}</Td>
                      <Td>{p.paid ? (p.config?.allow_paid ? "approved" : "off") : "—"}</Td>
                      <Td>{p.budget_daily_usd != null ? usd(p.budget_daily_usd) : "—"}</Td>
                      <Td className="text-xs">
                        {p.loaded == null ? "unknown" : p.loaded ? (p.healthy ? "healthy" : p.health_detail) : "no"}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <Empty>No provider rows: only the free providers (simulated, local) are in use.</Empty>
            )}
          </>
        ) : null}
      </CardContent>
    </Card>
  );
}

export default function GpuPage() {
  const me = useMe();
  const admin = Boolean(me.data?.user.is_platform_admin);
  return (
    <>
      <PageHeader
        title="GPU"
        description="The fleet: pools scale with the queue, idle workers stop, and budgets hold low-priority work."
      />
      <div className="flex flex-col gap-4">
        {admin ? <Queue /> : null}
        <Pools />
        <Workers admin={admin} />
        <Offers />
        {admin ? <Provision /> : null}
        {admin ? <Providers /> : null}
      </div>
    </>
  );
}
