import { Badge } from "@/components/ui/badge";
import { humanize } from "@/lib/format";

const TONES: Record<string, "success" | "warning" | "danger" | "info" | "muted"> = {
  ready: "success",
  previz_ready: "info",
  approved: "info",
  planned: "muted",
  planning: "muted",
  previz_running: "warning",
  generating: "warning",
  partial: "warning",
  needs_review: "warning",
  failed: "danger",
  cancelled: "muted",
  succeeded: "success",
  running: "warning",
  queued: "muted",
};

export function StateBadge({ state }: { state: string | null | undefined }) {
  if (!state) return <Badge variant="muted">—</Badge>;
  return (
    <Badge variant={TONES[state] ?? "outline"} data-state={state}>
      {humanize(state)}
    </Badge>
  );
}
