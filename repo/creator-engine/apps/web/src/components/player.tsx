"use client";
/** The version player: the final render, else the proxy; mock provenance is labeled (I9). */
import type { Ref } from "react";

import { Skeleton } from "@/components/ui/misc";
import { useRenderDownload, useRenders } from "@/lib/queries";

const BUILDING = new Set(["approved", "generating"]);

export function Player({
  versionId,
  state,
  videoRef,
  testId = "player",
}: {
  versionId: string;
  state: string;
  videoRef?: Ref<HTMLVideoElement>;
  testId?: string;
}) {
  const renders = useRenders(versionId);
  const final = (renders.data ?? []).find((r) => !r.is_proxy && r.status === "ready");
  const proxy = (renders.data ?? []).find((r) => r.is_proxy && r.status === "ready");
  const chosen = final ?? proxy ?? null;
  const download = useRenderDownload(chosen?.id ?? null);
  if (!chosen) {
    return (
      <div className="flex aspect-[9/16] max-h-[60vh] items-center justify-center rounded-md bg-slate-900 text-sm text-slate-200">
        {BUILDING.has(state)
          ? "Rendering…"
          : state === "previz_ready"
            ? "Approve the previz to generate."
            : ["planned", "previz_running"].includes(state)
              ? "Previz is under way…"
              : "No render yet."}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      {download.data ? (
        <video
          ref={videoRef}
          data-testid={testId}
          controls
          playsInline
          preload="metadata"
          src={download.data.url}
          className="max-h-[60vh] w-full rounded-md bg-black"
        />
      ) : (
        <Skeleton className="aspect-[9/16] max-h-[60vh]" />
      )}
      <p className="text-xs text-slate-600">
        {chosen.preset_id} · {chosen.is_proxy ? "proxy" : "final"} ·{" "}
        {chosen.provenance_mode === "mock_dev" ? "mock provenance (not exportable)" : chosen.provenance_mode}
      </p>
    </div>
  );
}
