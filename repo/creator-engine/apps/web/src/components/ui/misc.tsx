import * as React from "react";

import { cn } from "@/lib/utils";

export function Alert({
  className,
  tone = "info",
  ...props
}: React.HTMLAttributes<HTMLDivElement> & { tone?: "info" | "warning" | "danger" | "success" }) {
  const tones = {
    info: "border-blue-700 bg-blue-50 text-blue-950",
    warning: "border-amber-700 bg-amber-50 text-amber-950",
    danger: "border-red-700 bg-red-50 text-red-950",
    success: "border-green-700 bg-green-50 text-green-950",
  };
  return (
    // errors interrupt (role=alert); everything else is announced politely (role=status)
    <div
      role={tone === "danger" ? "alert" : "status"}
      className={cn("rounded-md border-l-4 px-3 py-2 text-sm", tones[tone], className)}
      {...props}
    />
  );
}

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div aria-hidden className={cn("animate-pulse rounded-md bg-slate-200", className)} {...props} />;
}

export function Progress({ value, label }: { value: number; label: string }) {
  const pct = Math.round(Math.min(1, Math.max(0, value)) * 100);
  return (
    <div className="flex items-center gap-2">
      <div
        role="progressbar"
        aria-label={label}
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        className="h-2 flex-1 overflow-hidden rounded-full bg-slate-200"
      >
        <div className="h-full bg-[var(--color-primary)] transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      <span className="w-10 text-right text-xs tabular-nums text-slate-700">{pct}%</span>
    </div>
  );
}

export function Table({ className, ...props }: React.TableHTMLAttributes<HTMLTableElement>) {
  // wide tables scroll inside their card instead of widening the page on small screens
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

export function Th({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      className={cn(
        "border-b border-[var(--color-border)] px-2 py-1.5 text-left font-medium text-slate-700",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b border-[var(--color-border)] px-2 py-1.5 align-top", className)} {...props} />;
}

export function Tabs<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: readonly { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  return (
    <div role="tablist" aria-label={label} className="flex gap-1 border-b border-[var(--color-border)]">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          role="tab"
          type="button"
          aria-selected={tab.id === value}
          onClick={() => onChange(tab.id)}
          className={cn(
            "-mb-px border-b-2 px-3 py-1.5 text-sm",
            tab.id === value
              ? "border-[var(--color-primary)] font-medium text-slate-900"
              : "border-transparent text-slate-600 hover:text-slate-900",
          )}
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="rounded-md border border-dashed border-slate-300 p-4 text-sm text-slate-600">{children}</p>;
}

/**
 * A query that failed: what could not be loaded, why, and a retry. Pages show this instead of their
 * empty state, so a failed request never reads as "nothing here".
 */
export function LoadError({ what, error, onRetry }: { what: string; error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <Alert tone="danger" className="flex flex-wrap items-center justify-between gap-2">
      <span>
        Could not load {what}: {message}
      </span>
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          className="rounded-md border border-red-700 px-2 py-0.5 text-xs font-medium hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-blue-700"
        >
          Retry
        </button>
      ) : null}
    </Alert>
  );
}
