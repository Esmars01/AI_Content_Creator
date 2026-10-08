"use client";
/**
 * Brand kits (Phase 12, §30 "Templates and brand"): name, logo (an uploaded image), colors, font
 * names and a caption style; the project's kit is the default brand of its new plans and its logo
 * is burned into final renders when the version's logo overlay is on. Archiving keeps old versions
 * rendering. Intro/outro, libraries and rules are V1 features and not offered.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { RoleNote } from "@/components/role-note";
import { AssetImage, ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, type Schemas, unwrap } from "@/lib/api";
import { keys, useBrandKits, useCreateOptions } from "@/lib/queries";
import { uploadAsset } from "@/lib/upload";
import { useCan } from "@/lib/roles";

type Kit = Schemas["BrandKitOut"];

async function uploadLogo(file: File): Promise<string> {
  return (await uploadAsset(file, { kind: "logo" })).id;
}

const DEFAULT_PRIMARY = "#1a73e8";
const DEFAULT_ACCENT = "#fbbc04";

function KitForm({ kit, onDone }: { kit?: Kit; onDone: () => void }) {
  const client = useQueryClient();
  const options = useCreateOptions();
  const can = useCan("write_content");
  const [name, setName] = useState(kit?.name ?? "");
  const [primary, setPrimary] = useState(String((kit?.colors as Record<string, string>)?.primary ?? DEFAULT_PRIMARY));
  const [accent, setAccent] = useState(String((kit?.colors as Record<string, string>)?.accent ?? DEFAULT_ACCENT));
  const [heading, setHeading] = useState(String((kit?.fonts as Record<string, string>)?.heading ?? ""));
  const [style, setStyle] = useState(kit?.caption_style_id ?? "");
  const [logo, setLogo] = useState<File | null>(null);
  const [logoInput, setLogoInput] = useState(0); // a new key clears the file input
  const [created, setCreated] = useState<string | null>(null);
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
    onMutate: () => setCreated(null),
    onSuccess: (row) => {
      void client.invalidateQueries({ queryKey: keys.brandKits() });
      if (!kit) {
        // a fresh form: a second click must not create the same kit again (D15)
        setName("");
        setPrimary(DEFAULT_PRIMARY);
        setAccent(DEFAULT_ACCENT);
        setHeading("");
        setStyle("");
        setLogo(null);
        setLogoInput((n) => n + 1);
        setCreated(row.name);
      }
      onDone();
    },
  });
  return (
    <form
      className="grid grid-cols-2 gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (can.allowed) save.mutate();
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
          key={logoInput}
          id="kit-logo"
          type="file"
          accept="image/png,image/jpeg,image/webp"
          onChange={(e) => setLogo(e.target.files?.[0] ?? null)}
        />
      </div>
      <div className="col-span-2">
        <ErrorNote error={save.error} />
        {created ? (
          <Alert tone="success" className="mb-2">
            Brand kit “{created}” created.
          </Alert>
        ) : null}
        <Button type="submit" disabled={save.isPending || !can.allowed} title={can.reason}>
          {kit ? "Save" : "Create brand kit"}
        </Button>
      </div>
    </form>
  );
}

export function BrandKitsManager() {
  const client = useQueryClient();
  const kits = useBrandKits();
  const can = useCan("write_content");
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
        <RoleNote />
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
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => archive.mutate(kit.id)}
                      disabled={archive.isPending || !can.allowed}
                      title={can.reason}
                    >
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
  const can = useCan("write_content");
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
        disabled={set.isPending || !can.allowed}
        title={can.reason}
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
