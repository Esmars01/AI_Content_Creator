"use client";
/** The version player: the final render, else the proxy; mock provenance is labeled (I9). */
import { type Ref, type SyntheticEvent, useRef } from "react";

import { Skeleton } from "@/components/ui/misc";
import { useRenderDownload, useRenders } from "@/lib/queries";

const BUILDING = new Set(["approved", "generating"]);
// states in which the build produced (or should have produced) a render
const BUILT = new Set(["ready", "partial", "needs_review"]);
const FINISH_POLLS = 30; // × 2 s after the build ended

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
  // Keep polling while the final render is still expected — while building, and for about a
  // minute after the build ended — so a missed SSE event (a reconnect, a sleeping tab) never
  // leaves the player waiting after the render is ready.
  const listed = useRenders(versionId, (renders, fetches) => {
    if ((renders ?? []).some((r) => !r.is_proxy && r.status === "ready")) return false;
    return BUILDING.has(state) || (BUILT.has(state) && fetches < FINISH_POLLS);
  });
  const final = (listed.data ?? []).find((r) => !r.is_proxy && r.status === "ready");
  const proxy = (listed.data ?? []).find((r) => r.is_proxy && r.status === "ready");
  const finishing = BUILT.has(state) && !final && (listed.isLoading || listed.dataUpdatedAt === 0 || listed.isFetching);
  const chosen = final ?? proxy ?? null;
  const download = useRenderDownload(chosen?.id ?? null);
  const recovered = useRef<string | null>(null);
  // A presigned URL that expired (or a transient storage error) fails the media element: fetch a
  // fresh URL once per URL and resume where playback was.
  const onError = (event: SyntheticEvent<HTMLVideoElement>) => {
    const url = download.data?.url ?? null;
    if (!url || recovered.current === url) return;
    recovered.current = url;
    const element = event.currentTarget;
    const at = element.currentTime;
    void download.refetch().then(() => {
      element.addEventListener("loadedmetadata", () => (element.currentTime = at), { once: true });
    });
  };
  if (!chosen) {
    return (
      <div className="flex aspect-[9/16] max-h-[60vh] items-center justify-center rounded-md bg-slate-900 text-sm text-slate-200">
        {BUILDING.has(state)
          ? "Rendering…"
          : finishing
            ? "Finishing the render…"
            : BUILT.has(state)
              ? "No render was produced for this version."
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
          onError={onError}
          className="max-h-[60vh] w-full rounded-md bg-black"
        />
      ) : (
        <Skeleton className="aspect-[9/16] max-h-[60vh]" />
      )}
      <p className="flex flex-wrap items-center gap-x-1 text-xs text-slate-600">
        <span>
          {chosen.preset_id} · {chosen.is_proxy ? "proxy" : "final"} ·{" "}
          {chosen.provenance_mode === "mock_dev" ? "mock provenance (not exportable)" : chosen.provenance_mode}
        </span>
        {/* the link is signed with an attachment disposition: following it saves the file */}
        {!chosen.is_proxy && download.data ? (
          <>
            ·{" "}
            <a className="text-blue-800 underline" href={download.data.url} download data-testid="download-render">
              Download
            </a>
          </>
        ) : null}
      </p>
    </div>
  );
}
