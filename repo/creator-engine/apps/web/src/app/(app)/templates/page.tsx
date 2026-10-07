"use client";
/**
 * Spec templates (Phase 12, §30 "Templates and brand"): partial specs of portable slots, saved from a
 * version or written as a body, composed from other templates, versioned (an edit saves the next
 * version), previewed and applied to a version as an ordinary edit proposal.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { RoleNote } from "@/components/role-note";
import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { TEMPLATE_KINDS, templateSlots } from "@/lib/phase12";
import { useTemplate, useTemplates } from "@/lib/queries";
import { useCan } from "@/lib/roles";

function SlotList({ body }: { body: Record<string, unknown> }) {
  const slots = templateSlots(body);
  if (!slots.length) return <span className="text-xs text-slate-600">no own values</span>;
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-3 text-xs">
      {slots.map((slot) => (
        <div key={slot.path} className="contents">
          <dt className="font-mono text-slate-700">{slot.path}</dt>
          <dd>{slot.value}</dd>
        </div>
      ))}
    </dl>
  );
}

function TemplateDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const client = useQueryClient();
  const template = useTemplate(id);
  const can = useCan("write_content");
  const [versionId, setVersionId] = useState("");
  const [preview, setPreview] = useState<{ operations: Record<string, unknown>[] } | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const [body, setBody] = useState("");
  const refresh = () => client.invalidateQueries({ queryKey: ["templates"] });
  const run = useMutation({
    mutationFn: (dry: boolean) =>
      unwrap(
        api.POST("/v1/spec-templates/{spec_template_id}:apply", {
          params: { path: { spec_template_id: id } },
          body: { version_id: versionId, preview: dry },
          headers: dry ? undefined : idempotencyKey(),
        }),
      ),
    onSuccess: (data, dry) => {
      if (dry) setPreview(data as never);
      else {
        // proposed: a second click must not propose it again; preview again to re-apply (D13)
        setPreview(null);
        setApplied((data as { edit_proposal_id: string }).edit_proposal_id);
      }
    },
  });
  const edit = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/v1/spec-templates/{spec_template_id}", {
          params: { path: { spec_template_id: id } },
          body: { body: JSON.parse(body) as Record<string, unknown> },
        }),
      ),
    onSuccess: () => {
      void refresh();
      onClose();
    },
  });
  const archive = useMutation({
    mutationFn: () =>
      unwrap(api.DELETE("/v1/spec-templates/{spec_template_id}", { params: { path: { spec_template_id: id } } })),
    onSuccess: () => {
      void refresh();
      onClose();
    },
  });
  if (template.isLoading || !template.data) return <Skeleton className="h-32" />;
  const t = template.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle>
          {t.name} <Badge variant="outline">{humanize(t.kind)}</Badge> <Badge variant="muted">v{t.version}</Badge>
          {t.latest ? null : <Badge variant="warning">older version</Badge>}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {t.description ? <p className="text-sm">{t.description}</p> : null}
        <section>
          <h3 className="text-sm font-medium">What applying it sets</h3>
          <SlotList body={t.effective as Record<string, unknown>} />
        </section>
        {t.composes_from.length ? (
          <p className="text-xs text-slate-600">
            Composed from {t.composes_from.length} template(s), then its own values.
          </p>
        ) : null}
        {t.conflicts.length ? (
          <Alert tone="warning">
            Conflicts (the later template wins):{" "}
            {t.conflicts.map((c) => `${c.path} → ${JSON.stringify(c.winner)}`).join("; ")}
          </Alert>
        ) : null}
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            run.mutate(true);
          }}
        >
          <div className="flex flex-1 flex-col gap-1">
            <Label htmlFor="apply-version">Apply to version (id)</Label>
            <Input
              id="apply-version"
              value={versionId}
              onChange={(e) => setVersionId(e.target.value.trim())}
              required
            />
          </div>
          <Button type="submit" variant="outline">
            Preview
          </Button>
          <Button
            type="button"
            onClick={() => run.mutate(false)}
            disabled={!preview?.operations.length || run.isPending || !can.allowed}
            title={can.reason}
          >
            Propose edit
          </Button>
        </form>
        <ErrorNote error={run.error ?? edit.error ?? archive.error} />
        {preview ? (
          preview.operations.length ? (
            <ul className="list-disc pl-5 text-xs">
              {preview.operations.map((op, i) => (
                <li key={i}>
                  <code>{String(op.op)}</code> {JSON.stringify({ ...op, op: undefined, reason: undefined })}
                </li>
              ))}
            </ul>
          ) : (
            <Alert tone="info">The version already has every value of this template.</Alert>
          )
        ) : null}
        {applied ? (
          <Alert tone="success">
            Proposed as an edit; review and apply it in the Video Studio (proposal <code>{applied}</code>).
          </Alert>
        ) : null}
        {t.latest ? (
          <details>
            <summary className="cursor-pointer text-sm">Edit (saves version {t.version + 1})</summary>
            <Textarea
              aria-label="Template body (JSON)"
              className="font-mono text-xs"
              value={body || JSON.stringify(t.body, null, 2)}
              onChange={(e) => setBody(e.target.value)}
            />
            <Button
              className="mt-2"
              size="sm"
              onClick={() => edit.mutate()}
              disabled={!body || edit.isPending || !can.allowed}
              title={can.reason}
            >
              Save new version
            </Button>
          </details>
        ) : null}
        <div className="flex gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => archive.mutate()}
            disabled={archive.isPending || !can.allowed}
            title={can.reason}
          >
            Archive
          </Button>
          <Button size="sm" variant="ghost" onClick={onClose}>
            Close
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

const EXAMPLE_BODY = '{\n  "captions": {"style_id": "bold_pop_highlight"}\n}';

function CreateTemplate() {
  const client = useQueryClient();
  const can = useCan("write_content");
  const [name, setName] = useState("");
  const [kind, setKind] = useState<(typeof TEMPLATE_KINDS)[number]>("caption");
  const [source, setSource] = useState<"version" | "body">("version");
  const [versionId, setVersionId] = useState("");
  const [paths, setPaths] = useState("");
  const [body, setBody] = useState(EXAMPLE_BODY);
  const [saved, setSaved] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/spec-templates", {
          body: (source === "version"
            ? {
                name,
                kind,
                from_version_id: versionId,
                paths: paths.trim() ? paths.split(/[\s,]+/).filter(Boolean) : null,
              }
            : { name, kind, body: JSON.parse(body) as Record<string, unknown> }) as never,
        }),
      ),
    onMutate: () => setSaved(null),
    onSuccess: (row) => {
      // a fresh form: a second click must not save the same template again (D15)
      setName("");
      setVersionId("");
      setPaths("");
      setBody(EXAMPLE_BODY);
      setSaved(row.name);
      void client.invalidateQueries({ queryKey: ["templates"] });
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>New template</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          className="grid grid-cols-2 gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (can.allowed) create.mutate();
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="tpl-name">Name</Label>
            <Input id="tpl-name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="tpl-kind">Kind</Label>
            <Select id="tpl-kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
              {TEMPLATE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {humanize(k)}
                </option>
              ))}
            </Select>
          </div>
          <div className="col-span-2 flex gap-3 text-sm">
            <label className="flex items-center gap-1">
              <input type="radio" checked={source === "version"} onChange={() => setSource("version")} /> Save from a
              version
            </label>
            <label className="flex items-center gap-1">
              <input type="radio" checked={source === "body"} onChange={() => setSource("body")} /> Write the values
            </label>
          </div>
          {source === "version" ? (
            <>
              <div className="flex flex-col gap-1">
                <Label htmlFor="tpl-version">Version id</Label>
                <Input
                  id="tpl-version"
                  value={versionId}
                  onChange={(e) => setVersionId(e.target.value.trim())}
                  required
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="tpl-paths">Only these slots (optional)</Label>
                <Input
                  id="tpl-paths"
                  placeholder="/captions /brand"
                  value={paths}
                  onChange={(e) => setPaths(e.target.value)}
                />
              </div>
            </>
          ) : (
            <div className="col-span-2 flex flex-col gap-1">
              <Label htmlFor="tpl-body">Values (JSON)</Label>
              <Textarea
                id="tpl-body"
                className="font-mono text-xs"
                value={body}
                onChange={(e) => setBody(e.target.value)}
              />
            </div>
          )}
          <div className="col-span-2">
            <ErrorNote error={create.error} />
            {saved ? (
              <Alert tone="success" className="mb-2">
                Template “{saved}” saved.
              </Alert>
            ) : null}
            <Button type="submit" disabled={create.isPending || !can.allowed} title={can.reason}>
              Save template
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function Compose({ ids }: { ids: string[] }) {
  const compose = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/spec-templates:compose", { body: { template_ids: ids } })),
  });
  return (
    <div className="flex flex-col gap-2">
      <Button size="sm" variant="outline" disabled={ids.length < 2} onClick={() => compose.mutate()}>
        Preview composition of {ids.length} selected
      </Button>
      <ErrorNote error={compose.error} />
      {compose.data ? (
        <div className="rounded border p-2">
          <SlotList body={compose.data.body as Record<string, unknown>} />
          {compose.data.conflicts.length ? (
            <p className="mt-1 text-xs text-amber-900">
              Conflicts: {compose.data.conflicts.map((c) => c.path).join(", ")} (later wins)
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export default function TemplatesPage() {
  const [kind, setKind] = useState<string>("");
  const [open, setOpen] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const templates = useTemplates(kind || null);
  const items = templates.data?.items ?? [];
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Templates"
        description={
          <>
            Reusable partial specs: captions, brand, camera, pacing, outputs. Brand kits live in{" "}
            <Link className="underline" href="/settings">
              Settings
            </Link>
            .
          </>
        }
      />
      <RoleNote />
      <div className="flex items-center gap-2">
        <Label htmlFor="kind-filter">Kind</Label>
        <Select id="kind-filter" className="w-48" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All</option>
          {TEMPLATE_KINDS.map((k) => (
            <option key={k} value={k}>
              {humanize(k)}
            </option>
          ))}
        </Select>
      </div>
      {open ? <TemplateDetail key={open} id={open} onClose={() => setOpen(null)} /> : null}
      <Card>
        <CardContent className="flex flex-col gap-2">
          {templates.isLoading ? (
            <Skeleton className="h-24" />
          ) : items.length ? (
            <>
              <Table>
                <thead>
                  <tr>
                    <Th />
                    <Th>Template</Th>
                    <Th>Kind</Th>
                    <Th>Version</Th>
                    <Th>Values</Th>
                    <Th>Saved</Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((t) => (
                    <tr key={t.id} data-testid="template-row">
                      <Td>
                        <input
                          type="checkbox"
                          aria-label={`Select ${t.name} for composition`}
                          checked={selected.includes(t.id)}
                          onChange={(e) =>
                            setSelected(e.target.checked ? [...selected, t.id] : selected.filter((x) => x !== t.id))
                          }
                        />
                      </Td>
                      <Td>
                        <button type="button" className="text-blue-800 hover:underline" onClick={() => setOpen(t.id)}>
                          {t.name}
                        </button>
                      </Td>
                      <Td>{humanize(t.kind)}</Td>
                      <Td>v{t.version}</Td>
                      <Td>
                        <SlotList body={t.body as Record<string, unknown>} />
                      </Td>
                      <Td className="text-xs">{when(t.created_at)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
              <Compose ids={selected} />
            </>
          ) : (
            <Empty>No templates yet.</Empty>
          )}
        </CardContent>
      </Card>
      <CreateTemplate />
    </div>
  );
}
