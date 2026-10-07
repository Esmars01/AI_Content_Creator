"use client";
/**
 * Create (§31): a ten-step stepper whose answers become Director constraints, then Plan. Every
 * option list comes from `GET /v1/create-options` (configuration), creators, worlds and voices
 * from the API; the frontend holds no business rules beyond display.
 */
import { useMutation } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { PageHeader } from "@/components/app-shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Alert, Skeleton } from "@/components/ui/misc";
import { api, ApiError, idempotencyKey, unwrap } from "@/lib/api";
import { type Aspect, EMPTY, type Form, type InputMode, STEPS, toRequest } from "@/lib/create-form";
import { humanize } from "@/lib/format";
import {
  useCreateOptions,
  useCreator,
  useCreators,
  useProjects,
  useSources,
  useVoices,
  useWorlds,
} from "@/lib/queries";
import { cn } from "@/lib/utils";

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint ? <p className="text-xs text-slate-600">{hint}</p> : null}
    </div>
  );
}

function CreateWizard() {
  const router = useRouter();
  const params = useSearchParams();
  const options = useCreateOptions();
  const projects = useProjects();
  const creators = useCreators();
  const worlds = useWorlds();
  const [form, setForm] = useState<Form>({ ...EMPTY, projectId: params.get("project") ?? "" });
  const [step, setStep] = useState(0);
  const creator = useCreator(form.creatorId || null);
  const voices = useVoices(form.creatorId || null);
  const set = <K extends keyof Form>(key: K, value: Form[K]) => setForm((f) => ({ ...f, [key]: value }));

  // the creator's default world is preselected (§31 step 3)
  const defaultWorld = creator.data?.current_version?.default_world_ids?.[0] ?? "";
  useEffect(() => {
    if (defaultWorld) setForm((f) => (f.worldId ? f : { ...f, worldId: defaultWorld }));
  }, [defaultWorld]);
  useEffect(() => {
    const first = projects.data?.items[0]?.id;
    if (first) setForm((f) => (f.projectId ? f : { ...f, projectId: first }));
  }, [projects.data]);

  const mode = options.data?.modes.find((m) => m.id === form.mode);
  const approvedCreators = (creators.data?.items ?? []).filter((c) => c.status === "active" && c.current_version_id);
  const language = options.data?.languages.find((l) => l.id === form.language);

  const plan = useMutation({
    mutationFn: async () => {
      let projectId = form.projectId;
      if (!projectId) {
        const created = await unwrap(api.POST("/v1/projects", { body: { name: "My videos", description: "" } }));
        projectId = created.id;
      }
      return unwrap(
        api.POST("/v1/projects/{project_id}/videos", {
          params: { path: { project_id: projectId } },
          headers: idempotencyKey(),
          body: toRequest(form),
        }),
      );
    },
    onSuccess: (accepted) =>
      router.push(`/videos/${accepted.video_id}/versions/${accepted.version_id}/previz?job=${accepted.job_id}`),
  });

  const canPlan = form.input.trim().length > 0;
  const issues = useMemo(() => (plan.error instanceof ApiError ? plan.error.issues : []), [plan.error]);

  if (options.isLoading) return <Skeleton className="h-64" />;
  if (!options.data) return <Alert tone="danger">The create options could not be loaded.</Alert>;
  const o = options.data;

  return (
    <div className="grid grid-cols-1 gap-6 md:grid-cols-[14rem_1fr]">
      <ol aria-label="Steps" className="flex flex-col gap-1">
        {STEPS.map((name, index) => (
          <li key={name}>
            <button
              type="button"
              onClick={() => setStep(index)}
              aria-current={index === step ? "step" : undefined}
              className={cn(
                "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm",
                index === step ? "bg-slate-200 font-medium" : "hover:bg-slate-100",
              )}
            >
              <span className="flex h-5 w-5 items-center justify-center rounded-full border border-slate-400 text-xs">
                {index + 1}
              </span>
              {name}
            </button>
          </li>
        ))}
      </ol>
      <Card>
        <CardHeader>
          <CardTitle>
            {step + 1}. {STEPS[step]}
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {step === 0 ? (
            <>
              <Field id="project" label="Project">
                <Select id="project" value={form.projectId} onChange={(e) => set("projectId", e.target.value)}>
                  {(projects.data?.items ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                  {!projects.data?.items.length ? <option value="">A new project, “My videos”</option> : null}
                </Select>
              </Field>
              <Field
                id="input"
                label="Idea, outline, notes or exact script"
                hint="URLs and pasted text in here are used as research (fetched safely, treated as data)."
              >
                <Textarea
                  id="input"
                  rows={8}
                  value={form.input}
                  onChange={(e) => set("input", e.target.value)}
                  placeholder="Create a 30-second TikTok explaining why most people misunderstand AI agents."
                />
              </Field>
              <div className="flex items-center gap-4">
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.inputMode === "exact_script"}
                    onChange={(e) => set("inputMode", e.target.checked ? "exact_script" : "auto")}
                  />
                  This is my exact script
                </label>
                {form.inputMode === "exact_script" ? <Badge variant="info">wording locked</Badge> : null}
                <Field id="input-mode" label="Input type">
                  <Select
                    id="input-mode"
                    value={form.inputMode}
                    onChange={(e) => set("inputMode", e.target.value as InputMode)}
                  >
                    {o.input_modes.map((m) => (
                      <option key={m} value={m}>
                        {m === "auto" ? "Detect automatically" : humanize(m)}
                      </option>
                    ))}
                  </Select>
                </Field>
              </div>
              <p className="text-sm text-slate-700">
                The Director detects the mode from your input when it plans; you can override it in step 4.
              </p>
            </>
          ) : null}

          {step === 1 ? (
            <Field id="creator" label="Creator" hint="Only approved creators can be cast.">
              <Select id="creator" value={form.creatorId} onChange={(e) => set("creatorId", e.target.value)}>
                <option value="">Let the Director choose</option>
                {approvedCreators.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </Select>
            </Field>
          ) : null}

          {step === 2 ? (
            <Field
              id="world"
              label="World"
              hint={
                defaultWorld ? "The creator's default world is preselected." : "Approved worlds of your organization."
              }
            >
              <Select id="world" value={form.worldId} onChange={(e) => set("worldId", e.target.value)}>
                <option value="">Let the Director choose</option>
                {(worlds.data?.items ?? [])
                  .filter((w) => w.status === "active" && w.current_version_id)
                  .map((w) => (
                    <option key={w.id} value={w.id}>
                      {w.name} ({humanize(w.kind)}){w.id === defaultWorld ? " — default" : ""}
                    </option>
                  ))}
              </Select>
            </Field>
          ) : null}

          {step === 3 ? (
            <>
              <Field id="mode" label="Mode" hint={mode?.description}>
                <Select id="mode" value={form.mode} onChange={(e) => set("mode", e.target.value)}>
                  <option value="">Detect from the input</option>
                  {o.modes.map((m) => (
                    <option key={m.id} value={m.id} disabled={!m.plannable}>
                      {m.label}
                      {m.maturity !== "production" ? ` (${m.maturity})` : ""}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id="aspect" label="Aspect">
                <Select id="aspect" value={form.aspect} onChange={(e) => set("aspect", e.target.value as Aspect | "")}>
                  <option value="">From the platform and mode</option>
                  {(mode?.aspects ?? o.aspects).map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </Select>
              </Field>
              <fieldset className="flex flex-col gap-1">
                <legend className="text-sm font-medium">Platforms</legend>
                <div className="flex flex-wrap gap-3">
                  {o.platforms.map((p) => (
                    <label key={p.id} className="flex items-center gap-1 text-sm">
                      <input
                        type="checkbox"
                        checked={form.platforms.includes(p.id)}
                        onChange={(e) =>
                          set(
                            "platforms",
                            e.target.checked ? [...form.platforms, p.id] : form.platforms.filter((x) => x !== p.id),
                          )
                        }
                      />
                      {p.label}
                    </label>
                  ))}
                </div>
              </fieldset>
            </>
          ) : null}

          {step === 4 ? (
            <>
              <Field id="camera" label="Camera profile">
                <Select id="camera" value={form.cameraProfile} onChange={(e) => set("cameraProfile", e.target.value)}>
                  <option value="">The mode's default</option>
                  {o.camera_profiles.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id="captions" label="Caption style">
                <Select id="captions" value={form.captionStyle} onChange={(e) => set("captionStyle", e.target.value)}>
                  <option value="">The default style</option>
                  {o.caption_styles.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field
                id="music"
                label="Music mood"
                hint="Free text for the music engine; suggestions from the Director."
              >
                <Input
                  id="music"
                  list="music-moods"
                  value={form.musicMood}
                  onChange={(e) => set("musicMood", e.target.value)}
                />
                <datalist id="music-moods">
                  {o.music_moods.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </Field>
            </>
          ) : null}

          {step === 5 ? (
            <Field id="voice" label="Voice" hint={form.creatorId ? undefined : "Choose a creator first (step 2)."}>
              <Select
                id="voice"
                value={form.voiceVersionId}
                disabled={!form.creatorId}
                onChange={(e) => set("voiceVersionId", e.target.value)}
              >
                <option value="">The creator&apos;s voice</option>
                {(voices.data ?? [])
                  .filter((v) => v.current_version_id)
                  .map((v) => (
                    <option key={v.id} value={v.current_version_id ?? ""}>
                      {v.name}
                    </option>
                  ))}
              </Select>
            </Field>
          ) : null}

          {step === 6 ? (
            <Field
              id="duration"
              label="Target duration (seconds)"
              hint={
                mode
                  ? `${mode.label}: ${mode.duration_s.min}–${mode.duration_s.max} s (default ${mode.duration_s.default} s).`
                  : "Leave empty to use what the input implies or the mode's default."
              }
            >
              <Input
                id="duration"
                type="number"
                min={1}
                max={600}
                value={form.duration}
                onChange={(e) => set("duration", e.target.value)}
              />
            </Field>
          ) : null}

          {step === 7 ? (
            <>
              <Field id="language" label="Language">
                <Select id="language" value={form.language} onChange={(e) => set("language", e.target.value)}>
                  <option value="">Detect from the input</option>
                  {o.languages.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.label} — {l.support}
                    </option>
                  ))}
                </Select>
              </Field>
              {language ? (
                <Badge
                  variant={
                    language.support === "production" ? "success" : language.support === "beta" ? "warning" : "danger"
                  }
                >
                  {language.support}
                </Badge>
              ) : null}
              {language?.support === "unsupported" ? (
                <Alert tone="warning">
                  {language.label} is not supported for speech yet: plans work, generated audio may be wrong.
                </Alert>
              ) : null}
            </>
          ) : null}

          {step === 8 ? (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field id="tier" label="Quality tier">
                <Select
                  id="tier"
                  value={form.qualityTier}
                  onChange={(e) => set("qualityTier", e.target.value as "draft" | "final")}
                >
                  {o.quality_tiers.map((t) => (
                    <option key={t} value={t}>
                      {humanize(t)}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id="takes" label="Takes per talking shot">
                <Input
                  id="takes"
                  type="number"
                  min={1}
                  max={4}
                  value={form.takes}
                  onChange={(e) => set("takes", Math.max(1, Math.min(4, Number(e.target.value) || 1)))}
                />
              </Field>
              <Field
                id="sources"
                label="Sources"
                hint="Closed book: claims may rest only on your own text and uploaded documents or notes."
              >
                <Select
                  id="sources"
                  value={form.sourcesPolicy}
                  onChange={(e) => set("sourcesPolicy", e.target.value as Form["sourcesPolicy"])}
                >
                  <option value="">Use what the input contains</option>
                  <option value="open">Open (research allowed)</option>
                  <option value="closed_book">Closed book (only my sources)</option>
                </Select>
              </Field>
              <SourcePicker projectId={form.projectId} chosen={form.sources} onChange={(ids) => set("sources", ids)} />
              <Field id="budget" label="Budget cap (USD)">
                <Input
                  id="budget"
                  type="number"
                  min={0}
                  step="0.01"
                  value={form.budget}
                  onChange={(e) => set("budget", e.target.value)}
                />
              </Field>
              <Field id="routing" label="Routing profile">
                <Select
                  id="routing"
                  value={form.routingProfile}
                  onChange={(e) => set("routingProfile", e.target.value)}
                >
                  <option value="">From the quality tier</option>
                  {o.routing_profiles.map((r) => (
                    <option key={r.id} value={r.id}>
                      {r.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id="pack" label="Strategy pack">
                <Select id="pack" value={form.strategyPack} onChange={(e) => set("strategyPack", e.target.value)}>
                  <option value="">The Director chooses</option>
                  {o.strategy_packs.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.label}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field id="intent-hints" label="Intent hints (one per line)">
                <Textarea
                  id="intent-hints"
                  rows={3}
                  value={form.intentHints}
                  onChange={(e) => set("intentHints", e.target.value)}
                />
              </Field>
              <Field id="acting-hints" label="Acting hints (one per line)">
                <Textarea
                  id="acting-hints"
                  rows={3}
                  value={form.actingHints}
                  onChange={(e) => set("actingHints", e.target.value)}
                  placeholder="starts excited, becomes skeptical, then serious before the CTA"
                />
              </Field>
            </div>
          ) : null}

          {step === 9 ? (
            <>
              <dl className="grid grid-cols-1 gap-x-4 gap-y-1 text-sm sm:grid-cols-[10rem_1fr]">
                <dt className="text-slate-600">Input</dt>
                <dd className="line-clamp-3 whitespace-pre-wrap">{form.input || "—"}</dd>
                <dt className="text-slate-600">Creator</dt>
                <dd>{approvedCreators.find((c) => c.id === form.creatorId)?.name ?? "Director chooses"}</dd>
                <dt className="text-slate-600">Mode</dt>
                <dd>{mode?.label ?? "Detected"}</dd>
                <dt className="text-slate-600">Duration</dt>
                <dd>{form.duration ? `${form.duration} s` : "From the input or the mode"}</dd>
                <dt className="text-slate-600">Language</dt>
                <dd>{language?.label ?? "Detected"}</dd>
              </dl>
              <p className="text-sm text-slate-700">
                Planning writes the script, scenes, intent and performance, then runs a previz (voice, timing and
                keyframes). Nothing is generated until you approve the previz.
              </p>
              {plan.error ? (
                <Alert tone="danger">
                  {plan.error.message}
                  {issues.length ? (
                    <ul className="mt-1 list-disc pl-5">
                      {issues.map((issue, i) => (
                        <li key={i}>
                          {issue.path ? <code>{issue.path}</code> : null} {issue.message}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                </Alert>
              ) : null}
              <Button onClick={() => plan.mutate()} disabled={!canPlan || plan.isPending} size="lg">
                {plan.isPending ? "Starting…" : "Plan video"}
              </Button>
              {!canPlan ? <p className="text-sm text-amber-900">Write an idea or a script in step 1 first.</p> : null}
            </>
          ) : null}

          <div className="flex justify-between border-t border-[var(--color-border)] pt-3">
            <Button variant="outline" disabled={step === 0} onClick={() => setStep((s) => Math.max(0, s - 1))}>
              Back
            </Button>
            {step < STEPS.length - 1 ? (
              <Button onClick={() => setStep((s) => Math.min(STEPS.length - 1, s + 1))}>Next</Button>
            ) : null}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

/** The project's ingested research sources the plan may cite (Phase 12). */
function SourcePicker({
  projectId,
  chosen,
  onChange,
}: {
  projectId: string;
  chosen: string[];
  onChange: (ids: string[]) => void;
}) {
  const sources = useSources(projectId || null);
  const ingested = (sources.data?.items ?? []).filter((s) => s.status === "ingested");
  if (!projectId || !ingested.length) return null;
  return (
    <fieldset className="col-span-2 flex flex-col gap-1">
      <legend className="text-sm font-medium">Research sources to use</legend>
      {ingested.map((source) => (
        <label key={source.id} className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={chosen.includes(source.id)}
            onChange={(e) =>
              onChange(e.target.checked ? [...chosen, source.id] : chosen.filter((id) => id !== source.id))
            }
          />
          {source.title || source.uri || source.kind}
          <span className="text-xs text-slate-600">
            {source.trust === "user_provided" ? "your source" : "web"} · {source.fact_count} facts
          </span>
        </label>
      ))}
    </fieldset>
  );
}

export default function CreatePage() {
  return (
    <>
      <PageHeader title="Create" description="From an idea or an exact script to a reviewed plan." />
      <Suspense fallback={<Skeleton className="h-64" />}>
        <CreateWizard />
      </Suspense>
    </>
  );
}
