"use client";
/** GPU (§31, Phase 9; operations console, production cutover): pools, workers with their real
 * telemetry and offers for members; providers (create, edit, test, approve spending, delete),
 * provisioning, worker start/stop/restart/refresh/terminate, orphans, enrollment and the live queue
 * for platform admins. Paid providers stay off until the owner approves spending (a noted change);
 * this page never enables them by itself and never offers a simulated provider by default. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { Providers } from "@/components/gpu/providers";
import { Provision } from "@/components/gpu/provision";
import { errorText, POLL_MS, Workers } from "@/components/gpu/workers";
import { StateBadge } from "@/components/state-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Alert, Empty, Progress, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { usd } from "@/lib/format";
import { holdLabel, poolState, spendState } from "@/lib/gpu";
import { useMe } from "@/lib/queries";

const gpuKeys = {
  pools: ["gpu", "pools"] as const,
  queue: ["gpu", "queue"] as const,
};

function Pools() {
  const pools = useQuery({
    queryKey: gpuKeys.pools,
    queryFn: () => unwrap(api.GET("/v1/gpu/pools")),
    retry: false,
    refetchInterval: POLL_MS,
  });
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

function Offers() {
  const [gpuClass, setGpuClass] = useState("");
  const [region, setRegion] = useState("");
  const offers = useQuery({
    queryKey: ["gpu", "offers", gpuClass, region],
    queryFn: () =>
      unwrap(
        api.GET("/v1/gpu/offers", {
          params: { query: { gpu_class: gpuClass.trim() || undefined, region: region.trim() || undefined } },
        }),
      ),
    retry: false,
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Offers</CardTitle>
        <CardDescription>
          Live offers of the configured providers (a marketplace search: prices change). Paid offers cost money when
          rented.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="offer-class">GPU class</Label>
            <Input id="offer-class" className="w-44" value={gpuClass} onChange={(e) => setGpuClass(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="offer-region">Region</Label>
            <Input id="offer-region" className="w-28" value={region} onChange={(e) => setRegion(e.target.value)} />
          </div>
          <Button variant="outline" disabled={offers.isFetching} onClick={() => void offers.refetch()}>
            {offers.isFetching ? "Searching…" : "Refresh"}
          </Button>
        </div>
        {offers.error ? <Alert tone="warning">{errorText(offers.error)}</Alert> : null}
        {offers.isLoading ? <Skeleton className="h-16" /> : null}
        {offers.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Provider</Th>
                <Th>Class</Th>
                <Th>Region</Th>
                <Th>VRAM</Th>
                <Th>Price</Th>
                <Th>Driver</Th>
                <Th>Available</Th>
              </tr>
            </thead>
            <tbody>
              {offers.data.map((o, index) => (
                <tr key={`${o.provider}-${o.gpu_class}-${o.region}-${index}`}>
                  <Td>
                    {o.provider} {o.paid ? <Badge variant="outline">paid</Badge> : null}
                    {o.spot ? <Badge variant="outline">interruptible</Badge> : null}
                  </Td>
                  <Td className="text-xs">{o.gpu_class}</Td>
                  <Td className="text-xs">{o.region}</Td>
                  <Td>{o.vram_gb} GB</Td>
                  <Td>{usd(o.price_per_hour_usd)}/h</Td>
                  <Td className="text-xs">{o.driver_version ?? "not reported"}</Td>
                  <Td>{o.available}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : offers.data ? (
          <Empty>No offers from the configured providers{gpuClass || region ? " for this filter" : ""}.</Empty>
        ) : null}
      </CardContent>
    </Card>
  );
}

function Queue() {
  const queue = useQuery({
    queryKey: gpuKeys.queue,
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/queue")),
    refetchInterval: POLL_MS,
  });
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

export default function GpuPage() {
  const me = useMe();
  const admin = Boolean(me.data?.user.is_platform_admin);
  return (
    <>
      <PageHeader
        title="GPU"
        description="The fleet: providers, workers and their telemetry, pools that scale with the queue, and budgets that hold low-priority work."
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
