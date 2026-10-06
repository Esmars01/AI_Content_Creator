"use client";
/**
 * Brand kits (Phase 12, §30 "Templates and brand"): name, logo (an uploaded image), colors, font
 * names and a caption style; the project's kit is the default brand of its new plans and its logo
 * is burned into final renders when the version's logo overlay is on. Archiving keeps old versions
 * rendering. Intro/outro, libraries and rules are V1 features and not offered.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AssetImage, ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, idempotencyKey, type Schemas, unwrap } from "@/lib/api";
import { keys, useBrandKits, useCreateOptions } from "@/lib/queries";

type Kit = Schemas["BrandKitOut"];

async function uploadLogo(file: File): Promise<string> {
  const initiated = await unwrap(
    api.POST("/v1/assets:initiate-upload", {
      body: { filename: file.name, mime: file.type || "image/png", bytes: file.size, kind: "logo" },
    }),
  );
  const plan = initiated.upload as {
    part_size: number;
    parts: { part_number: number; url: string; headers: Record<string, string> }[];
  };
  for (const part of plan.parts) {
    const chunk = file.slice((part.part_number - 1) * plan.part_size, part.part_number * plan.part_size);
    const put = await fetch(part.url, { method: "PUT", body: chunk, headers: part.headers });
    if (!put.ok) throw new Error(`upload failed (${put.status})`);
  }
  await unwrap(
    api.POST("/v1/assets/{asset_id}:complete", {
      params: { path: { asset_id: initiated.asset_id } },
      body: {},
      headers: idempotencyKey(),
    }),
  );
  return initiated.asset_id;
}

function KitForm({ kit, onDone }: { kit?: Kit; onDone: () => void }) {
  const client = useQueryClient();
  const options = useCreateOptions();
  const [name, setName] = useState(kit?.name ?? "");
  const [primary, setPrimary] = useState(String((kit?.colors as Record<string, string>)?.primary ?? "#1a73e8"));
  const [accent, setAccent] = useState(String((kit?.colors as Record<string, string>)?.accent ?? "#fbbc04"));
  const [heading, setHeading] = useState(String((kit?.fonts as Record<string, string>)?.heading ?? ""));
  const [style, setStyle] = useState(kit?.caption_style_id ?? "");
  const [logo, setLogo] = useState<File | null>(null);
  const styles = (options.data?.caption_styles ?? []) as { id: string; label?: string }[];
  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = {
        name,
        colors: { primary, accent },
        fonts: heading ? { heading } : {},
        caption_style_id: style || null,
      };
      if (logo) body.logo_asset_id = await uploadLogo(logo);
      return kit
        ? unwrap(
            api.PATCH("/v1/brand-kits/{brand_kit_id}", {
              params: { path: { brand_kit_id: kit.id } },
              body: body as never,
            }),
          )
        : unwrap(api.POST("/v1/brand-kits", { body: body as never }));
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.brandKits() });
      onDone();
    },
  });
  return (
    <form
      className="grid grid-cols-2 gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="col-span-2 flex flex-col gap-1">
        <Label htmlFor="kit-name">Name</Label>
        <Input id="kit-name" value={name} onChange={(e) => setName(e.target.value)} required />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="kit-primary">Primary color</Label>
        <Input id="kit-primary" type="color" value={primary} onChange={(e) => setPrimary(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="kit-accent">Accent color</Label>
        <Input id="kit-accent" type="color" value={accent} onChange={(e) => setAccent(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="kit-font">Heading font (name)</Label>
        <Input id="kit-font" value={heading} onChange={(e) => setHeading(e.target.value)} placeholder="Inter" />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor="kit-style">Caption style</Label>
        <Select id="kit-style" value={style} onChange={(e) => setStyle(e.target.value)}>
          <option value="">Creator default</option>
          {styles.map((s) => (
            <option key={s.id} value={s.id}>
              {s.label ?? s.id}
            </option>
          ))}
        </Select>
      </div>
      <div className="col-span-2 flex flex-col gap-1">
        <Label htmlFor="kit-logo">Logo (PNG, JPEG or WebP)</Label>
        <Input
          id="kit-logo"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          onChange={(e) => setLogo(e.target.files?.[0] ?? null)}
        />
      </div>
      <div className="col-span-2">
        <ErrorNote error={save.error} />
        <Button type="submit" disabled={save.isPending}>
          {kit ? "Save" : "Create brand kit"}
        </Button>
      </div>
    </form>
  );
}

export function BrandKitsManager() {
  const client = useQueryClient();
  const kits = useBrandKits();
  const [editing, setEditing] = useState<string | null>(null);
  const archive = useMutation({
    mutationFn: (id: string) =>
      unwrap(api.DELETE("/v1/brand-kits/{brand_kit_id}", { params: { path: { brand_kit_id: id } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brandKits() }),
  });
  const items = kits.data?.items ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Brand kits</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {kits.isLoading ? (
          <Skeleton className="h-24" />
        ) : items.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Kit</Th>
                <Th>Logo</Th>
                <Th>Colors</Th>
                <Th>Caption style</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {items.map((kit) => (
                <tr key={kit.id} data-testid="brand-kit-row">
                  <Td>
                    {kit.name}
                    {editing === kit.id ? <KitForm kit={kit} onDone={() => setEditing(null)} /> : null}
                  </Td>
                  <Td>
                    {kit.logo_asset_id ? (
                      <AssetImage assetId={kit.logo_asset_id} alt={`${kit.name} logo`} className="h-10" />
                    ) : (
                      "—"
                    )}
                  </Td>
                  <Td>
                    {Object.entries(kit.colors as Record<string, string>).map(([role, color]) => (
                      <span key={role} className="mr-1 inline-flex items-center gap-1 text-xs">
                        <span aria-hidden className="inline-block h-3 w-3 rounded" style={{ background: color }} />
                        {role}
                      </span>
                    ))}
                  </Td>
                  <Td className="text-xs">{kit.caption_style_id ?? "—"}</Td>
                  <Td className="flex gap-1">
                    <Button size="sm" variant="outline" onClick={() => setEditing(editing === kit.id ? null : kit.id)}>
                      {editing === kit.id ? "Close" : "Edit"}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => archive.mutate(kit.id)}>
                      Archive
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Empty>No brand kits yet.</Empty>
        )}
        <ErrorNote error={archive.error} />
        <h3 className="text-sm font-medium">New brand kit</h3>
        <KitForm onDone={() => undefined} />
      </CardContent>
    </Card>
  );
}

export function ProjectBrandKit({ projectId, current }: { projectId: string; current: string | null }) {
  const client = useQueryClient();
  const kits = useBrandKits();
  const set = useMutation({
    mutationFn: (kitId: string | null) =>
      unwrap(
        api.PATCH("/v1/projects/{project_id}", {
          params: { path: { project_id: projectId } },
          body: { brand_kit_id: kitId } as never,
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.project(projectId) }),
  });
  return (
    <div className="flex items-center gap-2 text-sm">
      <Label htmlFor="project-kit">Default brand kit</Label>
      <Select
        id="project-kit"
        className="w-56"
        value={current ?? ""}
        onChange={(e) => set.mutate(e.target.value || null)}
      >
        <option value="">None</option>
        {(kits.data?.items ?? []).map((kit) => (
          <option key={kit.id} value={kit.id}>
            {kit.name}
          </option>
        ))}
      </Select>
      {current ? <Badge variant="info">new plans use it</Badge> : null}
      <ErrorNote error={set.error} />
    </div>
  );
}
