"use client";
/**
 * Captions, packaging and the export dialog (Phase 12, §31 "Export dialog: packaging fields,
 * presets, aspect reframing preview, disclosure checklist, provenance status").
 *
 * - Captions: the version's caption files per language; translating adds a language (a new
 *   version builds it); translations stay `pending` until a person approves them.
 * - Packaging: Director stage 12's title, description, hashtags, CTA and thumbnails per platform,
 *   editable with live limit counters (limits labelled as platform rules or our design defaults).
 * - Export: render and preset, packaging, captions, the platform's disclosure checklist and the
 *   provenance status; the reasons it is disabled mirror the API's export rules.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, idempotencyKey, type Schemas, unwrap } from "@/lib/api";
import { humanize, when } from "@/lib/format";
import { claimSummary, exportBlockers, limitCheck, parseHashtags } from "@/lib/phase12";
import {
  keys,
  useCaptions,
  useClaims,
  useCreateOptions,
  useExports,
  usePackaging,
  usePlatforms,
  useRenders,
} from "@/lib/queries";
import { useCan } from "@/lib/roles";
import { usePendingVersions } from "@/lib/store";
import { startDownload } from "@/lib/utils";

type Platform = Schemas["PlatformOut"];
type PackagingRow = Schemas["PackagingOut"];
type CaptionRow = Schemas["CaptionOut"];

// ====================================================================== captions
/** One caption file as a download: the presigned link is fetched on click (it expires). */
function CaptionDownloadButton({ caption, onError }: { caption: CaptionRow; onError: (error: unknown) => void }) {
  const [busy, setBusy] = useState(false);
  const built = Boolean(caption.artifact_id);
  return (
    <button
      type="button"
      className="text-blue-800 underline disabled:text-slate-500 disabled:no-underline"
      aria-label={`Download ${caption.language} ${caption.format} captions`}
      title={built ? `Download captions.${caption.language}.${caption.format}` : "The caption file is not built yet"}
      disabled={!built || busy}
      onClick={async () => {
        setBusy(true);
        onError(null);
        try {
          const out = await unwrap(
            api.GET("/v1/captions/{caption_id}/download", { params: { path: { caption_id: caption.id } } }),
          );
          startDownload(out.url, out.filename);
        } catch (error) {
          onError(error);
        } finally {
          setBusy(false);
        }
      }}
    >
      {caption.format}
    </button>
  );
}

export function CaptionsPanel({ versionId, videoId }: { versionId: string; videoId: string }) {
  const client = useQueryClient();
  const captions = useCaptions(versionId);
  const options = useCreateOptions();
  const [language, setLanguage] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<unknown>(null);
  const can = useCan("write_content");
  const refresh = () => client.invalidateQueries({ queryKey: keys.captions(versionId) });
  const translate = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/versions/{version_id}/captions:translate", {
          params: { path: { version_id: versionId } },
          body: { language },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: (data) => {
      usePendingVersions.getState().expectVersion(data.new_version_id, data.job_id);
      setCreated(data.new_version_id);
    },
  });
  const review = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      approve
        ? unwrap(
            api.POST("/v1/captions/{caption_id}:approve", { params: { path: { caption_id: id } }, body: { note: "" } }),
          )
        : unwrap(
            api.POST("/v1/captions/{caption_id}:reject", {
              params: { path: { caption_id: id } },
              body: { note: "rejected in the studio" },
            }),
          ),
    onSuccess: refresh,
  });
  const rows = captions.data ?? [];
  const byLanguage = new Map<string, typeof rows>();
  for (const row of rows) byLanguage.set(row.language, [...(byLanguage.get(row.language) ?? []), row]);
  const languages = options.data?.languages ?? [];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Captions</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <ErrorNote error={translate.error ?? review.error ?? downloadError} />
        {created ? (
          <Alert tone="info">
            The translation builds in a new version.{" "}
            <a className="underline" href={`/videos/${videoId}?version=${created}`}>
              Open it
            </a>
            .
          </Alert>
        ) : null}
        {captions.isLoading ? (
          <Skeleton className="h-16" />
        ) : byLanguage.size ? (
          <Table>
            <thead>
              <tr>
                <Th>Language</Th>
                <Th>Files</Th>
                <Th>Review</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {[...byLanguage.entries()].map(([lang, files]) => {
                const first = files[0];
                const state = first?.review_state ?? "n/a";
                return (
                  <tr key={lang} data-testid="caption-language">
                    <Td>{lang}</Td>
                    <Td className="text-xs">
                      <span className="flex flex-wrap gap-2">
                        {files.map((f) => (
                          <CaptionDownloadButton key={f.id} caption={f} onError={setDownloadError} />
                        ))}
                      </span>
                    </Td>
                    <Td>
                      {state === "n/a" ? (
                        <span className="text-xs text-slate-600">spoken language</span>
                      ) : (
                        <Badge variant={state === "approved" ? "success" : state === "rejected" ? "danger" : "warning"}>
                          {state}
                        </Badge>
                      )}
                    </Td>
                    <Td className="flex gap-1">
                      {state !== "n/a" && first ? (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => review.mutate({ id: first.id, approve: true })}
                            disabled={review.isPending || !can.allowed}
                            title={can.reason}
                          >
                            Approve
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => review.mutate({ id: first.id, approve: false })}
                            disabled={review.isPending || !can.allowed}
                            title={can.reason}
                          >
                            Reject
                          </Button>
                        </>
                      ) : null}
                    </Td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
        ) : (
          <Empty>No caption files yet: they are written by the build.</Empty>
        )}
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (can.allowed) translate.mutate();
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="translate-language">Translate captions into</Label>
            <Select id="translate-language" value={language} onChange={(e) => setLanguage(e.target.value)} required>
              <option value="">Choose…</option>
              {languages
                .filter((l) => l.support !== "unsupported" && !byLanguage.has(l.id))
                .map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.label}
                    {l.support === "beta" ? " (beta)" : ""}
                  </option>
                ))}
            </Select>
          </div>
          <Button type="submit" disabled={!language || translate.isPending || !can.allowed} title={can.reason}>
            Translate
          </Button>
        </form>
        <p className="text-xs text-slate-600">
          Translated captions highlight by phrase and must be approved before they are exported.
        </p>
      </CardContent>
    </Card>
  );
}

// ====================================================================== packaging
function Counter({ text, max, source }: { text: string; max: number; source: string }) {
  const check = limitCheck(text, max, source);
  return (
    <span className={check.over ? "text-xs font-medium text-red-800" : "text-xs text-slate-600"}>
      {check.length}/{check.max}
      {source === "design_default" ? " (our default — platform limit not verified)" : " (platform rule)"}
    </span>
  );
}

function PackagingEditor({ row, platform }: { row: PackagingRow; platform: Platform | undefined }) {
  const client = useQueryClient();
  const [title, setTitle] = useState(row.title);
  const [description, setDescription] = useState(row.description);
  const [hashtags, setHashtags] = useState(row.hashtags.join(" "));
  const [cta, setCta] = useState(row.cta_text);
  const limits = row.limits as Record<string, number> & { sources?: Record<string, string> };
  const sources = limits.sources ?? {};
  const can = useCan("write_content");
  const refresh = () => client.invalidateQueries({ queryKey: keys.packaging(row.version_id) });
  // The editor remounts when the row changes (keyed on `updated_at`), so every save sends the text
  // as typed: choosing a thumbnail or approving never discards unsaved edits (D7, D8).
  const fields = { title, description, hashtags: parseHashtags(hashtags), cta_text: cta };
  const dirty =
    title !== row.title ||
    description !== row.description ||
    cta !== row.cta_text ||
    fields.hashtags.join(" ") !== row.hashtags.join(" ");
  const patch = (body: Record<string, unknown>) =>
    unwrap(
      api.PATCH("/v1/packaging/{packaging_id}", { params: { path: { packaging_id: row.id } }, body: body as never }),
    );
  const save = useMutation({ mutationFn: patch, onSuccess: refresh });
  const approve = useMutation({
    mutationFn: async () => {
      if (dirty) await patch(fields); // approve what is on screen, not the stored copy
      return unwrap(api.POST("/v1/packaging/{packaging_id}:approve", { params: { path: { packaging_id: row.id } } }));
    },
    onSettled: refresh,
  });
  const candidates = (row.thumbnail_candidates ?? []) as { artifact_id: string; text?: string; at_s?: number }[];
  const generator = row.generator as { kind?: string; fallback?: string };
  return (
    <div className="flex flex-col gap-2 border-t border-[var(--color-border)] pt-3" data-testid="packaging-editor">
      <div className="flex items-center gap-2">
        <h3 className="font-medium">{platform?.label ?? row.platform}</h3>
        <Badge variant={row.status === "approved" ? "success" : "muted"}>{row.status}</Badge>
        <Badge variant={generator.kind === "llm" ? "info" : "warning"} title={generator.fallback}>
          {generator.kind === "llm" ? "written by the LLM" : "template packaging"}
        </Badge>
      </div>
      {generator.kind !== "llm" && generator.fallback ? (
        <Alert tone="warning">No usable LLM answer ({generator.fallback}); the deterministic template was used.</Alert>
      ) : null}
      <ErrorNote error={save.error ?? approve.error} />
      <div className="flex flex-col gap-1">
        <Label htmlFor={`title-${row.id}`}>Title</Label>
        <Input id={`title-${row.id}`} value={title} onChange={(e) => setTitle(e.target.value)} />
        <Counter text={title} max={limits.title_max_chars ?? 0} source={sources.title_max_chars ?? "design_default"} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`description-${row.id}`}>Description</Label>
        <Textarea id={`description-${row.id}`} value={description} onChange={(e) => setDescription(e.target.value)} />
        <Counter
          text={description}
          max={limits.description_max_chars ?? 0}
          source={sources.description_max_chars ?? "design_default"}
        />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`hashtags-${row.id}`}>Hashtags</Label>
        <Input id={`hashtags-${row.id}`} value={hashtags} onChange={(e) => setHashtags(e.target.value)} />
        <span className="text-xs text-slate-600">
          {parseHashtags(hashtags).length}/{limits.hashtags_max} hashtags
        </span>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`cta-${row.id}`}>Call to action</Label>
        <Input id={`cta-${row.id}`} value={cta} onChange={(e) => setCta(e.target.value)} />
        <Counter text={cta} max={limits.cta_max_chars ?? 0} source="design_default" />
      </div>
      {candidates.length ? (
        <fieldset className="flex flex-col gap-1">
          <legend className="text-sm font-medium">Thumbnail</legend>
          <div className="flex flex-wrap gap-2">
            {candidates.map((c, i) => (
              <label key={c.artifact_id} className="flex items-center gap-1 text-xs">
                <input
                  type="radio"
                  name={`thumb-${row.id}`}
                  checked={row.thumbnail_artifact_ids.includes(c.artifact_id)}
                  disabled={!can.allowed}
                  title={can.reason}
                  onChange={() => save.mutate({ ...fields, thumbnail_artifact_id: c.artifact_id })}
                />
                <ThumbnailLink
                  packagingId={row.id}
                  artifactId={c.artifact_id}
                  label={`Candidate ${i + 1}: ${c.text ?? ""}`}
                />
              </label>
            ))}
          </div>
        </fieldset>
      ) : (
        <Alert tone="warning">No thumbnails: the version had no final render when it was packaged.</Alert>
      )}
      {(row.issues as { code: string; message: string }[]).filter((i) => i.code === "limit").length ? (
        <Alert tone="danger">
          {(row.issues as { code: string; message: string }[])
            .filter((i) => i.code === "limit")
            .map((i) => i.message)
            .join("; ")}
        </Alert>
      ) : null}
      <div className="flex gap-2">
        <Button
          variant="outline"
          onClick={() => save.mutate(fields)}
          disabled={save.isPending || !can.allowed}
          title={can.reason}
        >
          Save
        </Button>
        <Button
          onClick={() => approve.mutate()}
          disabled={approve.isPending || save.isPending || (row.status === "approved" && !dirty) || !can.allowed}
          title={can.reason}
        >
          {dirty ? "Save and approve" : "Approve"}
        </Button>
      </div>
    </div>
  );
}

function ThumbnailLink({ packagingId, artifactId, label }: { packagingId: string; artifactId: string; label: string }) {
  const [url, setUrl] = useState<string | null>(null);
  return url ? (
    <img src={url} alt={label} className="h-24 rounded border object-cover" />
  ) : (
    <button
      type="button"
      className="text-blue-800 underline"
      onClick={async () => {
        const out = await unwrap(
          api.GET("/v1/packaging/{packaging_id}/thumbnails/{artifact_id}", {
            params: { path: { packaging_id: packagingId, artifact_id: artifactId } },
          }),
        );
        setUrl(out.url);
      }}
    >
      {label}
    </button>
  );
}

export function PackagingPanel({ versionId, targets }: { versionId: string; targets: string[] }) {
  const client = useQueryClient();
  const platforms = usePlatforms();
  const [polling, setPolling] = useState(false);
  const packaging = usePackaging(versionId, polling);
  const known = platforms.data ?? [];
  const can = useCan("write_content");
  const [chosen, setChosen] = useState<string[]>(targets);
  const start = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/versions/{version_id}:package", {
          params: { path: { version_id: versionId } },
          body: { platforms: chosen },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: () => {
      setPolling(true);
      setTimeout(() => setPolling(false), 120_000);
      void client.invalidateQueries({ queryKey: keys.packaging(versionId) });
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Packaging</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <form
          className="flex flex-wrap items-end gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (can.allowed) start.mutate();
          }}
        >
          <fieldset className="flex flex-wrap gap-2">
            <legend className="sr-only">Platforms</legend>
            {known.map((p) => (
              <label key={p.id} className="flex items-center gap-1 text-sm">
                <input
                  type="checkbox"
                  checked={chosen.includes(p.id)}
                  onChange={(e) => setChosen(e.target.checked ? [...chosen, p.id] : chosen.filter((x) => x !== p.id))}
                />
                {p.label}
              </label>
            ))}
          </fieldset>
          <Button type="submit" disabled={!chosen.length || start.isPending || !can.allowed} title={can.reason}>
            Write packaging
          </Button>
        </form>
        <ErrorNote error={start.error} />
        {packaging.isLoading ? (
          <Skeleton className="h-24" />
        ) : (packaging.data ?? []).length ? (
          (packaging.data ?? []).map((row) => (
            <PackagingEditor
              key={`${row.id}-${row.updated_at}`}
              row={row}
              platform={known.find((p) => p.id === row.platform)}
            />
          ))
        ) : (
          <Empty>No packaging yet: write it after the video renders.</Empty>
        )}
      </CardContent>
    </Card>
  );
}

// ====================================================================== export dialog
export function ExportPanel({ versionId, versionState }: { versionId: string; versionState: string }) {
  const client = useQueryClient();
  const platforms = usePlatforms();
  const renders = useRenders(versionId);
  const packaging = usePackaging(versionId);
  const captions = useCaptions(versionId);
  const claims = useClaims(versionId);
  const exports = useExports(versionId);
  const can = useCan("write_content");
  const [platformId, setPlatformId] = useState("");
  const [renderId, setRenderId] = useState("");
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [done, setDone] = useState<Schemas["ExportDownloads"] | null>(null);
  const platform = (platforms.data ?? []).find((p) => p.id === platformId);
  const finals = (renders.data ?? []).filter((r) => !r.is_proxy);
  const render = finals.find((r) => r.id === renderId) ?? null;
  const pack = (packaging.data ?? []).find((p) => p.platform === platformId) ?? null;
  const translations = new Map<string, string>();
  for (const c of captions.data ?? []) if (c.review_state !== "n/a") translations.set(c.language, c.review_state);
  const pending = [...translations.entries()].filter(([, s]) => s !== "approved").map(([l]) => l);
  const blockers = exportBlockers({
    render,
    versionState,
    presets: (platform?.presets ?? []).map((p) => p.id),
    packaging: pack,
    requireApprovedPackaging: true,
    checklist: platform?.checklist ?? [],
    checked,
    blockingClaims: claimSummary((claims.data ?? []).filter((c) => c.in_version)).blocking,
    pendingTranslations: pending,
    chosenLanguages: null,
  });
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/v1/renders/{render_id}/exports", {
          params: { path: { render_id: renderId } },
          body: { platform: platformId, packaging_id: pack?.id ?? null, disclosure_checklist: checked },
          headers: idempotencyKey(),
        }),
      ),
    onSuccess: (data) => {
      setDone(data);
      void client.invalidateQueries({ queryKey: keys.exports(versionId) });
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>Export</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="grid grid-cols-2 gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="export-platform">Platform</Label>
            <Select
              id="export-platform"
              value={platformId}
              onChange={(e) => {
                setPlatformId(e.target.value);
                setChecked({});
              }}
            >
              <option value="">Choose…</option>
              {(platforms.data ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.label}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="export-render">Render (preset)</Label>
            <Select id="export-render" value={renderId} onChange={(e) => setRenderId(e.target.value)}>
              <option value="">Choose…</option>
              {finals.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.preset_id} · {r.aspect} · {r.provenance_mode === "real" ? "signed" : "mock provenance"}
                </option>
              ))}
            </Select>
          </div>
        </div>
        {render ? (
          <p className="text-sm">
            Provenance:{" "}
            {render.provenance_mode === "real" ? (
              <Badge variant="success">C2PA manifest and watermarks</Badge>
            ) : (
              <Badge variant="danger">MOCK PROVENANCE — NOT FOR DISTRIBUTION</Badge>
            )}
          </p>
        ) : null}
        {platform ? (
          <fieldset className="flex flex-col gap-1">
            <legend className="text-sm font-medium">Disclosure checklist ({platform.label})</legend>
            {platform.checklist.map((item) => (
              <label key={item.key} className="flex items-start gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={Boolean(checked[item.key])}
                  onChange={(e) => setChecked({ ...checked, [item.key]: e.target.checked })}
                />
                {item.label}
                {item.required ? <span className="text-xs text-slate-600">(required)</span> : null}
              </label>
            ))}
            <p className="text-xs text-slate-600">
              These are reminders for the person who uploads; platform rules are not verified here.
            </p>
          </fieldset>
        ) : null}
        {pending.length ? (
          <p className="text-xs text-slate-600">Not exported until approved: {pending.join(", ")} captions.</p>
        ) : null}
        {platformId && renderId && blockers.length ? (
          <Alert tone="warning">
            <ul className="list-disc pl-4">
              {blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </Alert>
        ) : null}
        <ErrorNote error={create.error} />
        <div>
          <Button
            onClick={() => create.mutate()}
            disabled={!platformId || !renderId || blockers.length > 0 || create.isPending || !can.allowed}
            title={can.reason}
          >
            Export
          </Button>
        </div>
        {done ? (
          <Alert tone="success">
            Exported. Downloads:{" "}
            {Object.entries(done.downloads ?? {}).map(([name, d]) => (
              <a key={name} className="mr-2 underline" href={d.url}>
                {name}
              </a>
            ))}
          </Alert>
        ) : null}
        {(exports.data ?? []).length ? (
          <Table>
            <thead>
              <tr>
                <Th>Exported</Th>
                <Th>Platform</Th>
                <Th>Preset</Th>
              </tr>
            </thead>
            <tbody>
              {(exports.data ?? []).map((e) => (
                <tr key={e.id}>
                  <Td className="text-xs">{when(e.created_at)}</Td>
                  <Td>{humanize(e.platform)}</Td>
                  <Td className="text-xs">{e.preset_id}</Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : null}
      </CardContent>
    </Card>
  );
}
