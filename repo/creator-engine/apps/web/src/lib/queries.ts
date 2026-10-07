"use client";
/** Server state (TanStack Query): one key per resource, hooks over the typed client. */
import { useQuery } from "@tanstack/react-query";

import { api, ApiError, type Schemas, unwrap } from "./api";

export const keys = {
  me: () => ["me"] as const,
  options: () => ["create-options"] as const,
  projects: () => ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  videosAll: () => ["videos"] as const,
  projectVideos: (projectId: string) => ["videos", "project", projectId] as const,
  video: (id: string) => ["videos", id] as const,
  versionsAll: () => ["versions"] as const,
  versions: (videoId: string) => ["versions", "video", videoId] as const,
  version: (id: string) => ["version", id] as const,
  previz: (id: string) => ["version", id, "previz"] as const,
  intent: (id: string) => ["version", id, "intent"] as const,
  storyboard: (id: string) => ["version", id, "storyboard"] as const,
  coverage: (id: string) => ["version", id, "coverage"] as const,
  behavior: (id: string, scene: string | null) => ["version", id, "behavior", scene] as const,
  renders: (id: string) => ["version", id, "renders"] as const,
  snapshots: (id: string) => ["version", id, "snapshots"] as const,
  download: (renderId: string) => ["render", renderId, "download"] as const,
  jobs: () => ["jobs"] as const,
  jobList: (status: string | null) => ["jobs", "list", status] as const,
  job: (id: string) => ["jobs", id] as const,
  creators: () => ["creators"] as const,
  creator: (id: string) => ["creators", id] as const,
  creatorVersion: (id: string) => ["creator-version", id] as const,
  voices: (creatorId: string | null) => ["voices", creatorId] as const,
  worlds: () => ["worlds"] as const,
  world: (id: string) => ["worlds", id] as const,
  worldVersion: (id: string) => ["world-version", id] as const,
  memory: (creatorId: string) => ["memory", creatorId] as const,
  edits: (versionId: string) => ["version", versionId, "edits"] as const,
  edit: (id: string) => ["edit", id] as const,
  takes: (versionId: string) => ["version", versionId, "takes"] as const,
  compare: (videoId: string, a: string, b: string) => ["compare", videoId, a, b] as const,
  vocabulary: () => ["vocabulary"] as const,
  // Phase 12
  sources: (projectId: string) => ["sources", projectId] as const,
  source: (id: string) => ["source", id] as const,
  claims: (versionId: string) => ["version", versionId, "claims"] as const,
  templates: (kind: string | null) => ["templates", kind] as const,
  template: (id: string) => ["template", id] as const,
  brandKits: () => ["brand-kits"] as const,
  platforms: () => ["platforms"] as const,
  captions: (versionId: string) => ["version", versionId, "captions"] as const,
  packaging: (versionId: string) => ["version", versionId, "packaging"] as const,
  exports: (versionId: string) => ["version", versionId, "exports"] as const,
  // Phase 11 — under the version, so a version's terminal state refreshes them with it
  qc: (versionId: string) => ["version", versionId, "qc"] as const,
  critiques: (versionId: string) => ["version", versionId, "critiques"] as const,
  versionConsistency: (versionId: string) => ["version", versionId, "consistency"] as const,
};

/**
 * How long before a presigned URL expires it is refreshed (the API signs for `presign_ttl_s`,
 * 15 min by default); `null` when the response has no expiry.
 */
export function refreshBefore(
  expiresAt: string | null | undefined,
  now = Date.now(),
  marginMs = 60_000,
): number | false {
  if (!expiresAt) return false;
  const at = Date.parse(expiresAt);
  if (Number.isNaN(at)) return false;
  return Math.max(5_000, at - now - marginMs);
}

/** 404s are an answer ("not there yet"), not a retryable failure. */
function retry(count: number, error: Error): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
  return count < 2;
}

const opts = { retry } as const;

export const useMe = () =>
  useQuery({ queryKey: keys.me(), queryFn: () => unwrap(api.GET("/v1/me")), retry: false, staleTime: 60_000 });

export const useCreateOptions = () =>
  useQuery({ queryKey: keys.options(), queryFn: () => unwrap(api.GET("/v1/create-options")), staleTime: 300_000 });

export const useProjects = () =>
  useQuery({
    queryKey: keys.projects(),
    queryFn: () => unwrap(api.GET("/v1/projects", { params: { query: { limit: 100 } } })),
    ...opts,
  });

export const useProject = (id: string) =>
  useQuery({
    queryKey: keys.project(id),
    queryFn: () => unwrap(api.GET("/v1/projects/{project_id}", { params: { path: { project_id: id } } })),
    ...opts,
  });

export const useProjectVideos = (projectId: string | null) =>
  useQuery({
    queryKey: keys.projectVideos(projectId ?? ""),
    enabled: Boolean(projectId),
    queryFn: () =>
      unwrap(
        api.GET("/v1/projects/{project_id}/videos", {
          params: { path: { project_id: projectId ?? "" }, query: { limit: 50 } },
        }),
      ),
    ...opts,
  });

export const useVideo = (id: string, poll = false) =>
  useQuery({
    queryKey: keys.video(id),
    queryFn: () => unwrap(api.GET("/v1/videos/{video_id}", { params: { path: { video_id: id } } })),
    refetchInterval: poll ? 3000 : false,
    ...opts,
  });

export const useVersions = (videoId: string) =>
  useQuery({
    queryKey: keys.versions(videoId),
    queryFn: () =>
      unwrap(
        api.GET("/v1/videos/{video_id}/versions", { params: { path: { video_id: videoId }, query: { limit: 50 } } }),
      ),
    ...opts,
  });

/** `pollWhile` keeps refetching (every 2 s) while it returns true, e.g. until planning finishes. */
export const useVersion = (id: string | null, pollWhile?: (version: Schemas["VersionOut"] | undefined) => boolean) =>
  useQuery({
    queryKey: keys.version(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/versions/{version_id}", { params: { path: { version_id: id ?? "" } } })),
    refetchInterval: (query) => (pollWhile?.(query.state.data) ? 2000 : false),
    ...opts,
  });

export const usePreviz = (id: string | null, poll = false) =>
  useQuery({
    queryKey: keys.previz(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/versions/{version_id}/previz", { params: { path: { version_id: id ?? "" } } })),
    refetchInterval: poll ? 3000 : false,
    ...opts,
  });

export const useIntent = (id: string | null) =>
  useQuery({
    queryKey: keys.intent(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/versions/{version_id}/intent", { params: { path: { version_id: id ?? "" } } })),
    ...opts,
  });

export const useStoryboard = (id: string | null) =>
  useQuery({
    queryKey: keys.storyboard(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/storyboard", { params: { path: { version_id: id ?? "" } } })),
    staleTime: 600_000, // presigned links last 15 minutes
    ...opts,
  });

export const useCoverage = (id: string | null) =>
  useQuery({
    queryKey: keys.coverage(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/coverage", { params: { path: { version_id: id ?? "" } } })),
    ...opts,
  });

export const useBehavior = (id: string | null, sceneKey: string | null) =>
  useQuery({
    queryKey: keys.behavior(id ?? "", sceneKey),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(
        api.GET("/v1/versions/{version_id}/behavior", {
          params: { path: { version_id: id ?? "" }, query: sceneKey ? { scene_key: sceneKey } : {} },
        }),
      ),
    ...opts,
  });

/**
 * A version's renders. `pollWhile(renders, fetches)` keeps refetching while the caller still
 * expects one, so the player never depends on an SSE event alone: events are a fast path, the
 * durable API state is the source of truth.
 */
export const useRenders = (id: string | null, pollWhile?: (data: Renders | undefined, fetches: number) => boolean) =>
  useQuery({
    queryKey: keys.renders(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/versions/{version_id}/renders", { params: { path: { version_id: id ?? "" } } })),
    refetchInterval: (query) => (pollWhile?.(query.state.data, query.state.dataUpdateCount) ? 2000 : false),
    ...opts,
  });

type Renders = Schemas["RenderOut"][];

/** A render's presigned download URL, refreshed a minute before it expires. */
export const useRenderDownload = (renderId: string | null) =>
  useQuery({
    queryKey: keys.download(renderId ?? ""),
    enabled: Boolean(renderId),
    queryFn: () =>
      unwrap(api.GET("/v1/renders/{render_id}/download", { params: { path: { render_id: renderId ?? "" } } })),
    staleTime: (query) => refreshBefore(query.state.data?.expires_at) || 600_000,
    refetchInterval: (query) => refreshBefore(query.state.data?.expires_at),
    ...opts,
  });

export const useSnapshots = (id: string | null) =>
  useQuery({
    queryKey: keys.snapshots(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/memory-snapshots", { params: { path: { version_id: id ?? "" } } })),
    ...opts,
  });

export const useJobs = (status: string | null = null, poll = false) =>
  useQuery({
    queryKey: keys.jobList(status),
    queryFn: () => unwrap(api.GET("/v1/jobs", { params: { query: { limit: 50, ...(status ? { status } : {}) } } })),
    refetchInterval: poll ? 5000 : false,
    ...opts,
  });

export const useJob = (id: string | null, poll = false) =>
  useQuery({
    queryKey: keys.job(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/jobs/{job_id}", { params: { path: { job_id: id ?? "" } } })),
    refetchInterval: poll ? 2000 : false,
    ...opts,
  });

export const useCreators = () =>
  useQuery({
    queryKey: keys.creators(),
    queryFn: () => unwrap(api.GET("/v1/creators", { params: { query: { limit: 100 } } })),
    ...opts,
  });

export const useCreator = (id: string | null) =>
  useQuery({
    queryKey: keys.creator(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/creators/{creator_id}", { params: { path: { creator_id: id ?? "" } } })),
    ...opts,
  });

export const useVoices = (creatorId: string | null) =>
  useQuery({
    queryKey: keys.voices(creatorId),
    enabled: Boolean(creatorId),
    queryFn: () => unwrap(api.GET("/v1/voices", { params: { query: { creator_id: creatorId ?? undefined } } })),
    ...opts,
  });

export const useWorlds = () =>
  useQuery({
    queryKey: keys.worlds(),
    queryFn: () => unwrap(api.GET("/v1/worlds", { params: { query: { limit: 100 } } })),
    ...opts,
  });

export const useWorld = (id: string | null) =>
  useQuery({
    queryKey: keys.world(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/worlds/{world_id}", { params: { path: { world_id: id ?? "" } } })),
    ...opts,
  });

export const useWorldVersion = (id: string | null) =>
  useQuery({
    queryKey: keys.worldVersion(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/world-versions/{world_version_id}", { params: { path: { world_version_id: id ?? "" } } })),
    ...opts,
  });

export const useMemory = (creatorId: string | null) =>
  useQuery({
    queryKey: keys.memory(creatorId ?? ""),
    enabled: Boolean(creatorId),
    queryFn: () =>
      unwrap(
        api.GET("/v1/creators/{creator_id}/memory", {
          params: { path: { creator_id: creatorId ?? "" }, query: { limit: 200 } },
        }),
      ),
    ...opts,
  });

export const useEdits = (versionId: string | null) =>
  useQuery({
    queryKey: keys.edits(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/edits", { params: { path: { version_id: versionId ?? "" } } })),
    ...opts,
  });

/** A proposal; polls (every 1.5 s) while it is still being proposed or applied. */
export const useEdit = (id: string | null) =>
  useQuery({
    queryKey: keys.edit(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/edits/{edit_proposal_id}", { params: { path: { edit_proposal_id: id ?? "" } } })),
    refetchInterval: (query) => (query.state.data?.status === "proposing" ? 1500 : false),
    ...opts,
  });

export const useTakes = (versionId: string | null) =>
  useQuery({
    queryKey: keys.takes(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/takes", { params: { path: { version_id: versionId ?? "" } } })),
    staleTime: 600_000, // presigned links
    ...opts,
  });

export const useCompare = (videoId: string, a: string | null, b: string | null) =>
  useQuery({
    queryKey: keys.compare(videoId, a ?? "", b ?? ""),
    enabled: Boolean(a && b),
    queryFn: () =>
      unwrap(
        api.GET("/v1/videos/{video_id}/compare", {
          params: { path: { video_id: videoId }, query: { a: a ?? "", b: b ?? "" } },
        }),
      ),
    ...opts,
  });

export const useVocabulary = () =>
  useQuery({ queryKey: keys.vocabulary(), queryFn: () => unwrap(api.GET("/v1/vocabulary")), staleTime: 3_600_000 });

export type Video = Schemas["VideoOut"];
export type Version = Schemas["VersionOut"];
export type Previz = Schemas["PrevizOut"];
export type Job = Schemas["JobOut"];

// ---------------------------------------------------------------------- Phase 12
export const useSources = (projectId: string | null, poll = false) =>
  useQuery({
    queryKey: keys.sources(projectId ?? ""),
    enabled: Boolean(projectId),
    queryFn: () =>
      unwrap(
        api.GET("/v1/projects/{project_id}/sources", {
          params: { path: { project_id: projectId ?? "" }, query: { limit: 100 } },
        }),
      ),
    refetchInterval: poll ? 3000 : false,
    ...opts,
  });

export const useSource = (id: string | null) =>
  useQuery({
    queryKey: keys.source(id ?? ""),
    enabled: Boolean(id),
    queryFn: () => unwrap(api.GET("/v1/sources/{source_id}", { params: { path: { source_id: id ?? "" } } })),
    ...opts,
  });

export const useClaims = (versionId: string | null) =>
  useQuery({
    queryKey: keys.claims(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/claims", { params: { path: { version_id: versionId ?? "" } } })),
    ...opts,
  });

export const useTemplates = (kind: string | null = null, allVersions = false) =>
  useQuery({
    queryKey: [...keys.templates(kind), allVersions],
    queryFn: () =>
      unwrap(
        api.GET("/v1/spec-templates", {
          params: { query: { kind: (kind ?? undefined) as never, all_versions: allVersions, limit: 100 } },
        }),
      ),
    ...opts,
  });

export const useTemplate = (id: string | null) =>
  useQuery({
    queryKey: keys.template(id ?? ""),
    enabled: Boolean(id),
    queryFn: () =>
      unwrap(api.GET("/v1/spec-templates/{spec_template_id}", { params: { path: { spec_template_id: id ?? "" } } })),
    ...opts,
  });

export const useBrandKits = () =>
  useQuery({
    queryKey: keys.brandKits(),
    queryFn: () => unwrap(api.GET("/v1/brand-kits", { params: { query: { limit: 100 } } })),
    ...opts,
  });

export const usePlatforms = () =>
  useQuery({ queryKey: keys.platforms(), queryFn: () => unwrap(api.GET("/v1/platforms")), staleTime: 300_000 });

export const useCaptions = (versionId: string | null, poll = false) =>
  useQuery({
    queryKey: keys.captions(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/captions", { params: { path: { version_id: versionId ?? "" } } })),
    refetchInterval: poll ? 4000 : false,
    ...opts,
  });

export const usePackaging = (versionId: string | null, poll = false) =>
  useQuery({
    queryKey: keys.packaging(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/packaging", { params: { path: { version_id: versionId ?? "" } } })),
    refetchInterval: poll ? 3000 : false,
    ...opts,
  });

export const useExports = (versionId: string | null) =>
  useQuery({
    queryKey: keys.exports(versionId ?? ""),
    enabled: Boolean(versionId),
    queryFn: () =>
      unwrap(api.GET("/v1/versions/{version_id}/exports", { params: { path: { version_id: versionId ?? "" } } })),
    ...opts,
  });
