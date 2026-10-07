"use client";
/**
 * Cancel for a queued or running job (`POST /v1/jobs/{id}:cancel`). Cancelling stops the workflow;
 * nodes that already finished stay in the build manifest, so a resume reuses them.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote } from "@/components/studio/common";
import { Button } from "@/components/ui/button";
import { api, unwrap } from "@/lib/api";
import { keys } from "@/lib/queries";
import { useCan } from "@/lib/roles";

export const CANCEL_CONFIRM = "Cancel this job? Work already done is kept.";
const CANCELLABLE = new Set(["queued", "running"]);

export function CancelJobButton({ job }: { job: { id: string; status: string; video_version_id?: string | null } }) {
  const client = useQueryClient();
  const can = useCan("write_content");
  // the API answers "cancelling"; the job stays running until the workflow stops
  const [requested, setRequested] = useState(false);
  const cancel = useMutation({
    mutationFn: () => unwrap(api.POST("/v1/jobs/{job_id}:cancel", { params: { path: { job_id: job.id } } })),
    onSuccess: () => setRequested(true),
    onSettled: async () => {
      await client.invalidateQueries({ queryKey: keys.jobs() }); // the job and every job list
      await client.invalidateQueries({ queryKey: keys.versionsAll() });
      if (job.video_version_id) await client.invalidateQueries({ queryKey: keys.version(job.video_version_id) });
    },
  });
  if (!CANCELLABLE.has(job.status)) return null;
  return (
    <span className="flex flex-col items-end gap-1">
      <Button
        size="sm"
        variant="outline"
        data-testid="cancel-job"
        disabled={cancel.isPending || requested || !can.allowed}
        title={can.reason}
        onClick={() => {
          if (window.confirm(CANCEL_CONFIRM)) cancel.mutate();
        }}
      >
        {cancel.isPending || requested ? "Cancelling…" : "Cancel"}
      </Button>
      <ErrorNote error={cancel.error} />
    </span>
  );
}
