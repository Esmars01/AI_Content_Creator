"use client";
import { seconds } from "@/lib/format";
import type { Lane } from "@/lib/performance";
import { humanize } from "@/lib/format";

/** Read-only Performance lane: emotion bands, transition markers and event markers (§31). */
export function PerformanceLane({ lane, measured }: { lane: Lane; measured: boolean }) {
  return (
    <figure aria-label="Performance timeline" className="flex flex-col gap-1">
      <div
        className="relative h-10 overflow-hidden rounded-md border border-slate-300 bg-slate-100"
        data-testid="performance-lane"
      >
        {lane.bands.map((band) => (
          <div
            key={band.key}
            className="absolute top-0 flex h-full items-center overflow-hidden border-r border-white px-1 text-xs font-medium text-white"
            style={{ left: `${band.left}%`, width: `${band.width}%`, background: band.color }}
            title={`${humanize(band.label)}${band.masking && band.felt ? ` (hiding ${band.felt})` : ""} · ${seconds(band.startS)}–${seconds(band.endS)}${band.driftS !== null ? ` · drift ${band.driftS >= 0 ? "+" : ""}${band.driftS.toFixed(2)} s` : ""}`}
          >
            <span className="truncate">{humanize(band.label)}</span>
          </div>
        ))}
        {lane.transitions.map((marker) => (
          <div
            key={marker.key}
            aria-hidden
            className="absolute top-0 h-full w-0.5 bg-slate-900"
            style={{ left: `${marker.left}%` }}
            title={marker.label}
          />
        ))}
      </div>
      <div className="relative h-5" aria-label="Behavior events">
        {lane.events.map((marker) => (
          <span
            key={marker.key}
            className="absolute -translate-x-1/2 text-[10px] text-slate-800"
            style={{ left: `${marker.left}%` }}
            title={`${marker.label} at ${seconds(marker.atS, 2)}`}
          >
            ▲
          </span>
        ))}
      </div>
      <figcaption className="flex justify-between text-xs text-slate-600">
        <span>0 s</span>
        <span>{measured ? "measured after previz" : "estimated"}</span>
        <span>{seconds(lane.durationS)}</span>
      </figcaption>
      {lane.events.length ? (
        <ul className="flex flex-wrap gap-2 text-xs text-slate-700">
          {lane.events.map((marker) => (
            <li key={marker.key}>
              ▲ {marker.label} · {seconds(marker.atS, 1)}
            </li>
          ))}
        </ul>
      ) : null}
    </figure>
  );
}
