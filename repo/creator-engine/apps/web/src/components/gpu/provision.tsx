"use client";
/** Provision a worker now (platform admins). No default provider or class: the operator picks a
 * configured provider, a GPU class from its live offers, a family and a region. A paid provider shows
 * the cheapest live price and needs an explicit acknowledgement before anything is rented. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useMemo, useState } from "react";

import { errorText } from "@/components/gpu/workers";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Alert } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { usd, when } from "@/lib/format";
import { provisionChoices } from "@/lib/gpu";

const FAMILIES = ["wan", "tts", "image", "asr", "audio", "lipsync", "vision", "post", "vllm", "cpu_model"] as const;
type Family = (typeof FAMILIES)[number];

export function Provision() {
  const client = useQueryClient();
  const providers = useQuery({
    queryKey: ["gpu", "providers"],
    queryFn: () => unwrap(api.GET("/v1/admin/gpu/providers")),
  });
  const choices = provisionChoices(providers.data);
  const [choice, setChoice] = useState("");
  const [gpuClass, setGpuClass] = useState("");
  const [region, setRegion] = useState("");
  const [family, setFamily] = useState<Family | "">("");
  const [variant, setVariant] = useState("");
  const [count, setCount] = useState(1);
  const [acknowledged, setAcknowledged] = useState(false);
  const selected = choices.find((c) => c.value === choice);
  const providerKey = selected
    ? (providers.data?.rows.find((r) => r.id === selected.providerId)?.kind ?? choice.replace(/^key:/, ""))
    : "";
  const offers = useQuery({
    queryKey: ["gpu", "offers", "provision", providerKey],
    enabled: Boolean(providerKey),
    queryFn: () => unwrap(api.GET("/v1/gpu/offers")),
    retry: false,
  });
  const providerOffers = useMemo(
    () => (offers.data ?? []).filter((o) => o.provider === providerKey),
    [offers.data, providerKey],
  );
  const classes = [...new Set(providerOffers.map((o) => o.gpu_class))].sort();
  const regions = [
    ...new Set(providerOffers.filter((o) => !gpuClass || o.gpu_class === gpuClass).map((o) => o.region)),
  ].sort();
  const cheapest = providerOffers
    .filter((o) => o.gpu_class === gpuClass && (!region || o.region === region))
    .reduce<number | null>((min, o) => (min === null || o.price_per_hour_usd < min ? o.price_per_hour_usd : min), null);
  const run = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/admin/gpu/workers:provision", {
          body: {
            ...(selected?.providerId ? { provider_id: selected.providerId } : { provider: providerKey }),
            gpu_class: gpuClass,
            runtime_family: family as Family,
            count,
            region: region || null,
            variant: variant.trim() || null,
          },
        }),
      ),
    onSettled: () => client.invalidateQueries({ queryKey: ["gpu"] }),
  });
  const enroll = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/admin/gpu/workers:enroll", { body: { runtime_family: family as Family } })),
  });
  const ready = Boolean(selected && gpuClass && family && (!selected.paid || acknowledged));
  function submit(event: FormEvent) {
    event.preventDefault();
    if (ready) run.mutate();
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Provision or enroll</CardTitle>
        <CardDescription>
          Rent workers now on a configured provider, or issue a one-time token for a self-managed host (WORKER_TOKEN).
          The worker boots the family image, registers with the scheduler and prepares its models.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form className="flex flex-col gap-2" onSubmit={submit}>
          <div className="flex flex-wrap items-end gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="pv-provider">Provider</Label>
              <Select
                id="pv-provider"
                value={choice}
                onChange={(e) => {
                  setChoice(e.target.value);
                  setGpuClass("");
                  setRegion("");
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
              <Label htmlFor="pv-class">GPU class</Label>
              {classes.length ? (
                <Select id="pv-class" value={gpuClass} onChange={(e) => setGpuClass(e.target.value)}>
                  <option value="">Choose a class</option>
                  {classes.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              ) : (
                <Input
                  id="pv-class"
                  className="w-48"
                  value={gpuClass}
                  placeholder={providerKey ? "no live offers: type a class" : ""}
                  onChange={(e) => setGpuClass(e.target.value)}
                />
              )}
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="pv-region">Region</Label>
              <Select id="pv-region" value={region} onChange={(e) => setRegion(e.target.value)}>
                <option value="">Any offered region</option>
                {regions.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="pv-family">Runtime family</Label>
              <Select id="pv-family" value={family} onChange={(e) => setFamily(e.target.value as Family)}>
                <option value="">Choose a family</option>
                {FAMILIES.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="pv-variant">Image variant</Label>
              <Input
                id="pv-variant"
                className="w-36"
                value={variant}
                placeholder="e.g. infinitetalk"
                onChange={(e) => setVariant(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="pv-count">Count</Label>
              <Input
                id="pv-count"
                className="w-20"
                type="number"
                min={1}
                max={8}
                value={count}
                onChange={(e) => setCount(Math.max(1, Math.min(8, Number(e.target.value) || 1)))}
              />
            </div>
          </div>
          {selected?.paid ? (
            <label
              className="flex items-start gap-2 rounded border border-amber-300 bg-amber-50 p-2 text-sm"
              htmlFor="pv-ack"
            >
              <input
                id="pv-ack"
                type="checkbox"
                className="mt-1"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>
                This rents {count} paid GPU instance{count > 1 ? "s" : ""}
                {cheapest !== null ? ` from about ${usd(cheapest)}/h each` : ""}; billing starts now and continues until
                it is stopped or terminated (a stopped instance may still bill storage).
              </span>
            </label>
          ) : null}
          <div className="flex gap-2">
            <Button type="submit" disabled={!ready || run.isPending}>
              {run.isPending ? "Provisioning…" : "Provision"}
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={!family || enroll.isPending}
              onClick={() => enroll.mutate()}
            >
              Issue enrollment token
            </Button>
          </div>
        </form>
        {offers.error ? <Alert tone="warning">Offers unavailable: {errorText(offers.error)}</Alert> : null}
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
