"use client";
/** The GPU workers table (cutover §8, §19): every worker that is not destroyed, stopped ones
 * included (they keep their disk and can be started again), with real telemetry only — a value the
 * worker did not report says so — and, for platform admins, the actions its state allows. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ConfirmAction } from "@/components/gpu/confirm";
import { StateBadge } from "@/components/state-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Empty, Skeleton, Table, Tabs, Td, Th } from "@/components/ui/misc";
import { api, ApiError, unwrap } from "@/lib/api";
import { humanize, usd } from "@/lib/format";
import {
  accruedText,
  ageText,
  coldStart,
  modelStates,
  providerText,
  telemetryFacts,
  type Worker,
  WORKER_SCOPES,
  type WorkerScope,
} from "@/lib/gpu";

export const POLL_MS = 5_000;

export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.problem?.detail ?? error.message;
  return error instanceof Error ? error.message : String(error);
}

type Action = "start" | "restart" | "refresh" | "stop" | "terminate" | "prepare" | "cancel-prepare";

/** A prepare the worker has been asked for and not finished: some model is still on its way. */
function preparing(worker: Worker): boolean {
  const request = (worker.prepare_request ?? {}) as { id?: string; cancel?: boolean };
  if (!request.id || request.cancel) return false;
  const states = Object.values((worker.model_states ?? {}) as Record<string, { state?: string }>);
  return states.some((s) => s.state === "downloading" || s.state === "verifying" || s.state === "loading");
}

function WorkerActions({ worker, run, busy }: { worker: Worker; run: (a: Action) => void; busy: boolean }) {
  const actions = worker.actions ?? [];
  return (
    <div className="flex flex-col items-start gap-1">
      {actions.includes("refresh") ? (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => run("refresh")}>
          Refresh
        </Button>
      ) : null}
      {actions.includes("start") ? (
        <Button size="sm" disabled={busy} onClick={() => run("start")}>
          Start
        </Button>
      ) : null}
      {["idle", "busy", "provisioning"].includes(worker.state) && actions.includes("refresh") ? (
        preparing(worker) ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => run("cancel-prepare")}>
            Cancel prepare
          </Button>
        ) : (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => run("prepare")}
            title="Download, verify and load this worker's models now (once per cache)"
          >
            Prepare models
          </Button>
        )
      ) : null}
      {actions.includes("restart") ? (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => run("restart")}>
          Restart
        </Button>
      ) : null}
      {actions.includes("stop") ? (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => run("stop")}>
          Stop
        </Button>
      ) : null}
      {actions.includes("terminate") ? (
        <ConfirmAction
          label="Terminate"
          phrase="terminate"
          disabled={busy}
          warning="Terminating destroys the instance and its disk (the model cache on it is lost). Billing stops."
          onConfirm={() => run("terminate")}
        />
      ) : null}
    </div>
  );
}

export function Workers({ admin }: { admin: boolean }) {
  const client = useQueryClient();
  const [scope, setScope] = useState<WorkerScope>("active");
  const workers = useQuery({
    queryKey: ["gpu", "workers", scope],
    queryFn: () => unwrap(api.GET("/v1/gpu/workers", { params: { query: { scope } } })),
    refetchInterval: POLL_MS,
  });
  const act = useMutation({
    mutationFn: async ({ id, action }: { id: string; action: Action }) => {
      if (action === "prepare")
        return unwrap(
          api.POST("/v1/admin/gpu/workers/{worker_id}:prepare", {
            params: { path: { worker_id: id } },
            body: { warm: true },
          }),
        );
      if (action === "cancel-prepare")
        return unwrap(
          api.POST("/v1/admin/gpu/workers/{worker_id}:cancel-prepare", { params: { path: { worker_id: id } } }),
        );
      if (action === "stop" || action === "terminate")
        return unwrap(
          api.POST("/v1/admin/gpu/workers/{worker_id}:stop", {
            params: { path: { worker_id: id } },
            body: action === "terminate" ? { action: "terminate", confirm: id } : { action: "stop" },
          }),
        );
      const path = {
        start: "/v1/admin/gpu/workers/{worker_id}:start",
        restart: "/v1/admin/gpu/workers/{worker_id}:restart",
        refresh: "/v1/admin/gpu/workers/{worker_id}:refresh",
      } as const;
      return unwrap(api.POST(path[action], { params: { path: { worker_id: id } } }));
    },
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workers</CardTitle>
        <CardDescription>
          Telemetry comes from the workers themselves (refreshed every few seconds); a value a worker did not report
          says &ldquo;not reported&rdquo;. Stopped workers keep their disk and model cache and can be started again; a
          stopped instance may still bill storage at its provider.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Tabs tabs={WORKER_SCOPES} value={scope} onChange={setScope} label="Worker states" />
        {act.error ? <Alert tone="danger">{errorText(act.error)}</Alert> : null}
        {workers.error ? <Alert tone="warning">{errorText(workers.error)}</Alert> : null}
        {workers.isLoading ? <Skeleton className="h-24" /> : null}
        {workers.data?.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Worker</Th>
                <Th>Scheduler state</Th>
                <Th>GPU and price</Th>
                <Th>Worker telemetry</Th>
                <Th>Models</Th>
                <Th>Heartbeat</Th>
                {admin ? <Th>Actions</Th> : null}
              </tr>
            </thead>
            <tbody>
              {workers.data.map((w) => (
                <tr key={w.id} className="align-top">
                  <Td>
                    <div className="font-mono text-xs">{w.external_id ?? w.id}</div>
                    <div className="text-xs text-slate-600">
                      {w.provider_kind ?? "self-managed"} · {humanize(w.runtime_family)}
                      {w.variant ? `:${w.variant}` : ""} · {w.pool_id ?? "no pool"}
                    </div>
                    <div className="text-xs text-slate-600">Provider view: {providerText(w)}</div>
                  </Td>
                  <Td>
                    <StateBadge state={w.state} />
                    {w.current_task_id ? <div className="text-xs">task {w.current_task_id.slice(0, 8)}</div> : null}
                    {w.last_error ? <div className="max-w-56 text-xs text-red-800">{w.last_error}</div> : null}
                  </Td>
                  <Td className="text-xs">
                    <div>
                      {w.gpu_type}
                      {w.region ? ` · ${w.region}` : ""}
                    </div>
                    <div>{usd(w.price_per_hour_usd)}/h</div>
                    {accruedText(w) ? <div>{accruedText(w)}</div> : null}
                    <div>Cold start: {coldStart(w)}</div>
                  </Td>
                  <Td className="text-xs">
                    {telemetryFacts(w).map((f) => (
                      <div key={f.label}>
                        {f.label}: {f.value}
                      </div>
                    ))}
                  </Td>
                  <Td className="text-xs">
                    {modelStates(w).length
                      ? modelStates(w).map((m) => (
                          <div key={m.key}>
                            {m.key}: {humanize(m.state)}
                            {m.detail ? ` (${m.detail})` : ""}
                          </div>
                        ))
                      : "none reported"}
                  </Td>
                  <Td className="text-xs">{ageText(w.last_heartbeat_at)}</Td>
                  {admin ? (
                    <Td>
                      <WorkerActions
                        worker={w}
                        busy={act.isPending}
                        run={(action) => act.mutate({ id: w.id, action })}
                      />
                    </Td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </Table>
        ) : workers.data ? (
          <Empty>No workers in this view.</Empty>
        ) : null}
      </CardContent>
    </Card>
  );
}
