/** The Create wizard's form and its mapping to `POST /v1/projects/{id}/videos` (display logic only). */
import type { Schemas } from "./api";

export type CreateVideo = Schemas["CreateVideo"];
export type Aspect = "9:16" | "16:9" | "1:1" | "4:5";
export type InputMode = CreateVideo["input_mode"];

export const STEPS = [
  "Idea or script",
  "Creator",
  "World",
  "Format",
  "Style",
  "Voice",
  "Duration",
  "Language",
  "Advanced",
  "Plan",
] as const;

export interface Form {
  projectId: string;
  input: string;
  inputMode: InputMode;
  creatorId: string;
  worldId: string;
  mode: string;
  aspect: Aspect | "";
  platforms: string[];
  cameraProfile: string;
  captionStyle: string;
  musicMood: string;
  voiceVersionId: string;
  duration: string;
  language: string;
  qualityTier: "draft" | "final";
  takes: number;
  sourcesPolicy: "" | "open" | "closed_book";
  sources: string[];
  budget: string;
  routingProfile: string;
  strategyPack: string;
  intentHints: string;
  actingHints: string;
}

export const EMPTY: Form = {
  projectId: "",
  input: "",
  inputMode: "auto",
  creatorId: "",
  worldId: "",
  mode: "",
  aspect: "",
  platforms: [],
  cameraProfile: "",
  captionStyle: "",
  musicMood: "",
  voiceVersionId: "",
  duration: "",
  language: "",
  qualityTier: "draft",
  takes: 1,
  sourcesPolicy: "",
  sources: [],
  budget: "",
  routingProfile: "",
  strategyPack: "",
  intentHints: "",
  actingHints: "",
};

export const lines = (text: string) =>
  text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 10);

/**
 * The form with one field changed, and the choices that belonged to the old value cleared: a voice
 * is the chosen creator's (D11), research sources are the chosen project's.
 */
export function withField<K extends keyof Form>(form: Form, key: K, value: Form[K]): Form {
  const next = { ...form, [key]: value };
  if (key === "creatorId" && value !== form.creatorId) next.voiceVersionId = "";
  if (key === "projectId" && value !== form.projectId) next.sources = [];
  return next;
}

/**
 * Why the target duration cannot be sent, or null; empty leaves it to the input or the mode. The
 * API takes more than 0 and at most 600 seconds; the wizard sent anything and showed the schema
 * error (audit BREAK-DURATION).
 */
export function durationProblem(duration: string): string | null {
  if (!duration.trim()) return null;
  const s = Number(duration);
  return Number.isFinite(s) && s > 0 && s <= 600 ? null : "Set a target duration between 1 and 600 seconds in step 7.";
}

/** The request body for the form; empty choices are left to the Director. */
export function toRequest(form: Form): CreateVideo {
  return {
    input: form.input,
    input_mode: form.inputMode,
    cast: form.creatorId
      ? [{ creator_id: form.creatorId, role: "host", voice_version_id: form.voiceVersionId || null }]
      : [],
    world_id: form.worldId || null,
    mode: form.mode || null,
    platform_targets: form.platforms,
    primary_aspect: form.aspect || null,
    target_duration_s: form.duration ? Number(form.duration) : null,
    language: form.language || null,
    quality_tier: form.qualityTier,
    style: {
      camera_profile_id: form.cameraProfile || null,
      caption_style_id: form.captionStyle || null,
      music_mood: form.musicMood || null,
    },
    sources_policy: form.sourcesPolicy || null,
    sources: form.sources,
    budget_usd: form.budget ? Number(form.budget) : null,
    advanced: {
      takes: form.takes,
      routing_profile: form.routingProfile || null,
      strategy_pack: form.strategyPack || null,
      intent_hints: lines(form.intentHints),
      acting_hints: lines(form.actingHints),
    },
  };
}
