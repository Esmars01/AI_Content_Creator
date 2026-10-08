/**
 * The one upload flow (§30 assets): initiate → PUT each presigned part → complete → wait until the
 * server's validation job marks the asset `ready` (or `rejected`). Identity, world and voice
 * endpoints refuse an asset that is not ready yet, so callers use the asset only after this resolves.
 */
import { api, idempotencyKey, type Schemas, unwrap } from "@/lib/api";

export type UploadedAsset = Schemas["AssetDetail"];
export type UploadStage = "uploading" | "validating" | "ready";

export class UploadRejectedError extends Error {
  constructor(
    message: string,
    readonly reasons: string[],
  ) {
    super(message);
    this.name = "UploadRejectedError";
  }
}

const MIME_BY_EXTENSION: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  wav: "audio/wav",
  mp3: "audio/mpeg",
  flac: "audio/flac",
  mp4: "video/mp4",
  mov: "video/quicktime",
  webm: "video/webm",
  srt: "application/x-subrip",
  vtt: "text/vtt",
  md: "text/markdown",
  txt: "text/plain",
  pdf: "application/pdf",
};

/** The browser's type, else one from the extension (some browsers leave `type` empty). */
export function mimeOf(file: File, fallback = "application/octet-stream"): string {
  if (file.type) return file.type;
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return MIME_BY_EXTENSION[extension] ?? fallback;
}

/** Why the validation job rejected an asset, from its probe (`probe.rejected`). */
export function rejectionReasons(asset: UploadedAsset): string[] {
  const rejected = (asset.probe as { rejected?: { message?: string; code?: string }[] } | undefined)?.rejected;
  return (rejected ?? []).map((r) => r.message ?? r.code ?? "rejected");
}

export async function uploadAsset(
  file: File,
  {
    kind,
    projectId,
    tags,
    onStage,
    pollMs = 750,
    timeoutMs = 180_000,
    sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)),
  }: {
    kind: string;
    projectId?: string | null;
    tags?: string[];
    onStage?: (stage: UploadStage) => void;
    pollMs?: number;
    timeoutMs?: number;
    sleep?: (ms: number) => Promise<void>;
  },
): Promise<UploadedAsset> {
  onStage?.("uploading");
  const initiated = await unwrap(
    api.POST("/v1/assets:initiate-upload", {
      body: {
        filename: file.name,
        mime: mimeOf(file),
        bytes: file.size,
        kind,
        ...(projectId ? { project_id: projectId } : {}),
        ...(tags ? { tags } : {}),
      },
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
  onStage?.("validating");
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const asset = await unwrap(
      api.GET("/v1/assets/{asset_id}", { params: { path: { asset_id: initiated.asset_id } } }),
    );
    if (asset.status === "ready") {
      onStage?.("ready");
      return asset;
    }
    if (asset.status === "rejected") {
      const reasons = rejectionReasons(asset);
      throw new UploadRejectedError(`the file was rejected: ${reasons.join("; ") || "invalid media"}`, reasons);
    }
    if (Date.now() > deadline) throw new Error("the uploaded file is still being validated; try again in a moment");
    await sleep(pollMs);
  }
}
