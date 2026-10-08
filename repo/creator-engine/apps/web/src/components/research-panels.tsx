"use client";
/**
 * Research (Phase 12, §30): a project's persistent sources — URLs (fetched through the SSRF guard
 * on the server), uploaded documents (PDF, DOCX, HTML, text, SRT/VTT) and pasted notes — with
 * their ingestion state and facts; and the claim ledger of a version with evidence quotes and
 * overrides (open book only). Source text is shown as data, never rendered as markup.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { claimAction, claimSummary, sourceStatus } from "@/lib/phase12";
import { keys, useClaims, useSource, useSources } from "@/lib/queries";
import { mimeOf, uploadAsset } from "@/lib/upload";
import { useCan } from "@/lib/roles";

const DOCUMENT_TYPES: Record<string, "pdf" | "doc" | "transcript" | "note"> = {
  "application/pdf": "pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc",
  "text/html": "doc",
  "text/vtt": "transcript",
  "application/x-subrip": "transcript",
  "text/plain": "note",
  "text/markdown": "note",
};

async function uploadDocument(file: File): Promise<string> {
  const mime = mimeOf(file, file.name.endsWith(".srt") ? "application/x-subrip" : "text/plain");
  const typed = mime === file.type ? file : new File([file], file.name, { type: mime });
  return (await uploadAsset(typed, { kind: "document" })).id;
}

function SourceFacts({ sourceId }: { sourceId: string }) {
  const source = useSource(sourceId);
  if (source.isLoading) return <Skeleton className="h-16" />;
  const facts = source.data?.facts ?? [];
  if (!facts.length) return <Empty>No facts extracted.</Empty>;
  return (
    <ol className="flex max-h-64 list-decimal flex-col gap-1 overflow-auto pl-5 text-xs" aria-label="Facts">
      {facts.map((fact) => (
        <li key={fact.id} className="whitespace-pre-wrap">
          {fact.text}
        </li>
      ))}
    </ol>
  );
}

export function SourcesPanel({ projectId }: { projectId: string }) {
  const client = useQueryClient();
  const [mode, setMode] = useState<"url" | "note" | "file">("url");
  const [uri, setUri] = useState("");
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  // a file input cannot be cleared by state: a new key remounts it empty after each add (D12)
  const [fileInput, setFileInput] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const sources = useSources(projectId, true);
  const can = useCan("write_content");
  const refresh = () => client.invalidateQueries({ queryKey: keys.sources(projectId) });
  const add = useMutation({
    mutationFn: async () => {
      let body: Record<string, unknown>;
      if (mode === "url") body = { kind: "url", uri, title };
      else if (mode === "note") body = { kind: "note", text, title };
      else {
        if (!file) throw new Error("choose a file");
        const kind = DOCUMENT_TYPES[file.type] ?? (file.name.endsWith(".srt") ? "transcript" : "note");
        body = { kind, asset_id: await uploadDocument(file), title: title || file.name };
      }
      return unwrap(
        api.POST("/v1/projects/{project_id}/sources", {
          params: { path: { project_id: projectId } },
          body: body as never,
          headers: idempotencyKey(),
        }),
      );
    },
    onSuccess: () => {
      setUri("");
      setText("");
      setTitle("");
      setFile(null);
      setFileInput((n) => n + 1);
      void refresh();
    },
  });
  const act = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "reingest" | "delete" }) =>
      action === "delete"
        ? unwrap(api.DELETE("/v1/sources/{source_id}", { params: { path: { source_id: id } } }))
        : unwrap(api.POST("/v1/sources/{source_id}:reingest", { params: { path: { source_id: id } } })),
    onSuccess: refresh,
  });
  const items = sources.data?.items ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Research sources</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-xs text-slate-600">
          Facts from these sources can back the script&apos;s claims. Closed-book videos use only uploaded documents and
          notes (your own sources), never web pages. Everything read is treated as data, never as instructions.
        </p>
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (can.allowed) add.mutate();
          }}
        >
          <div className="flex gap-2">
            <Select
              aria-label="Source type"
              className="w-40"
              value={mode}
              onChange={(e) => setMode(e.target.value as typeof mode)}
            >
              <option value="url">Web page (URL)</option>
              <option value="file">Document upload</option>
              <option value="note">Pasted note</option>
            </Select>
            <Input
              aria-label="Title"
              placeholder="Title (optional)"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </div>
          {mode === "url" ? (
            <Input
              aria-label="URL"
              placeholder="https://…"
              value={uri}
              onChange={(e) => setUri(e.target.value)}
              required
            />
          ) : mode === "note" ? (
            <Textarea aria-label="Note text" value={text} onChange={(e) => setText(e.target.value)} required />
          ) : (
            <Input
              key={fileInput}
              aria-label="Document"
              type="file"
              accept=".pdf,.docx,.html,.htm,.txt,.md,.srt,.vtt"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              required
            />
          )}
          <div>
            <Button type="submit" disabled={add.isPending || !can.allowed} title={can.reason}>
              {add.isPending ? "Adding…" : "Add source"}
            </Button>
          </div>
        </form>
        <ErrorNote error={add.error ?? act.error} />
        {sources.isLoading ? (
          <Skeleton className="h-24" />
        ) : items.length ? (
          <Table>
            <thead>
              <tr>
                <Th>Source</Th>
                <Th>Trust</Th>
                <Th>State</Th>
                <Th>Facts</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {items.map((source) => {
                const status = sourceStatus(source.status, source.error as never);
                return (
                  <tr key={source.id} data-testid="source-row">
                    <Td>
                      <div className="font-medium">{source.title || source.uri || humanize(source.kind)}</div>
                      <div className="text-xs text-slate-600">
                        {humanize(source.kind)}
                        {source.uri ? ` · ${source.uri}` : ""} · added {when(source.created_at)}
                      </div>
                      {open === source.id ? <SourceFacts sourceId={source.id} /> : null}
                    </Td>
                    <Td>
                      <Badge variant={source.trust === "user_provided" ? "info" : "muted"}>
                        {humanize(source.trust)}
                      </Badge>
                    </Td>
                    <Td>
                      <Badge variant={status.tone}>{status.label}</Badge>
                      {source.embedding_model ? <div className="text-xs text-slate-600">embedded</div> : null}
                    </Td>
                    <Td>{source.fact_count}</Td>
                    <Td className="flex flex-wrap gap-1">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setOpen(open === source.id ? null : source.id)}
                      >
                        {open === source.id ? "Hide facts" : "Facts"}
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => act.mutate({ id: source.id, action: "reingest" })}
                        disabled={act.isPending || !can.allowed}
                        title={can.reason}
                      >
                        Re-ingest
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => act.mutate({ id: source.id, action: "delete" })}
                        disabled={act.isPending || !can.allowed}
                        title={can.reason}
                      >
                        Delete
                      </Button>
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <Empty>No sources yet.</Empty>
        )}
      </CardContent>
    </Card>
  );
}

const VERDICT_TONE: Record<string, "success" | "danger" | "warning" | "muted"> = {
  supported: "success",
  unsupported: "danger",
  uncertain: "warning",
  conflicting: "danger",
};

export function ClaimLedger({ versionId }: { versionId: string }) {
  const client = useQueryClient();
  const claims = useClaims(versionId);
  const can = useCan("write_content");
  const [reason, setReason] = useState<Record<string, string>>({});
  const override = useMutation({
    mutationFn: ({ id, why }: { id: string; why: string }) =>
      unwrap(api.POST("/v1/claims/{claim_id}:override", { params: { path: { claim_id: id } }, body: { reason: why } })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.claims(versionId) }),
  });
  if (claims.isLoading) return <Skeleton className="h-24" />;
  const rows = (claims.data ?? []).filter((c) => c.in_version);
  const summary = claimSummary(rows);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Claim ledger</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {rows.length ? (
          <>
            <p className="text-sm" aria-live="polite">
              {Object.entries(summary.counts)
                .map(([verdict, n]) => `${n} ${verdict}`)
                .join(" · ")}
              {summary.blocking ? ` — ${summary.blocking} block approval and export` : ""}
            </p>
            {summary.blocking > summary.overridable ? (
              <Alert tone="warning">
                Closed-book claims cannot be overridden: edit the script or add one of your own sources that supports
                them, then replan.
              </Alert>
            ) : null}
            <ErrorNote error={override.error} />
            <Table>
              <thead>
                <tr>
                  <Th>Claim</Th>
                  <Th>Verdict</Th>
                  <Th>Evidence</Th>
                  <Th />
                </tr>
              </thead>
              <tbody>
                {rows.map((claim) => {
                  const action = claimAction(claim);
                  const evidence = (claim.evidence ?? []) as { quote?: string; text?: string; source_title?: string }[];
                  return (
                    <tr key={claim.id} data-testid="claim-row">
                      <Td>
                        <div>{claim.text}</div>
                        <div className="text-xs text-slate-600">
                          {claim.segment_key ?? ""} {claim.detected ? "· found by the checker" : ""}
                          {claim.closed_book ? " · closed book" : ""}
                        </div>
                        {(claim.reasons ?? []).length ? (
                          <div className="text-xs text-slate-600">{(claim.reasons as string[]).join("; ")}</div>
                        ) : null}
                      </Td>
                      <Td>
                        <Badge variant={VERDICT_TONE[claim.verdict] ?? "muted"}>{claim.verdict}</Badge>
                        {claim.blocking && !claim.override_by ? (
                          <Badge variant="danger" className="ml-1">
                            blocking
                          </Badge>
                        ) : null}
                      </Td>
                      <Td className="max-w-md text-xs">
                        {evidence.length ? (
                          <ul className="flex flex-col gap-1">
                            {evidence.map((e, i) => (
                              <li key={i}>
                                <q className="whitespace-pre-wrap">{e.quote ?? e.text}</q>
                                {e.source_title ? <span className="text-slate-600"> — {e.source_title}</span> : null}
                              </li>
                            ))}
                          </ul>
                        ) : (
                          "—"
                        )}
                      </Td>
                      <Td>
                        {action === "override" ? (
                          <form
                            className="flex gap-1"
                            onSubmit={(e) => {
                              e.preventDefault();
                              if (can.allowed) override.mutate({ id: claim.id, why: reason[claim.id] ?? "" });
                            }}
                          >
                            <Input
                              aria-label={`Override reason for ${claim.claim_key}`}
                              placeholder="Why it is true"
                              className="h-8 w-48 text-xs"
                              value={reason[claim.id] ?? ""}
                              onChange={(e) => setReason({ ...reason, [claim.id]: e.target.value })}
                              minLength={3}
                              required
                            />
                            <Button
                              size="sm"
                              variant="outline"
                              type="submit"
                              disabled={override.isPending || !can.allowed}
                              title={can.reason}
                            >
                              Override
                            </Button>
                          </form>
                        ) : action === "overridden" ? (
                          <span className="text-xs">Overridden: {claim.override_reason}</span>
                        ) : action === "closed_book" ? (
                          <span className="text-xs text-slate-600">Edit the script or add a source</span>
                        ) : null}
                      </Td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          </>
        ) : (
          <Empty>No checkable claims in this version.</Empty>
        )}
      </CardContent>
    </Card>
  );
}
