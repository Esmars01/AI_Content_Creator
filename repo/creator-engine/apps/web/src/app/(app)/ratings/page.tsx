"use client";
/** Ratings (§16.8, §20; Phase 11): the human evaluation queue. Sampled consistency reports ask "same
 * person?" and "in character?" (1–5); low-reliability checks (VLM window questions, relation and
 * lighting checks) ask whether the behavior or expectation is really there. Human ratings are the
 * ground truth those checks are calibrated against. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { PageHeader } from "@/components/app-shell";
import { RoleNote } from "@/components/role-note";
import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, Skeleton } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { humanize } from "@/lib/format";
import { useCan } from "@/lib/roles";

type Item = {
  target_type: "consistency" | "take" | "render";
  target_id: string;
  question_key: string;
  prompt: string;
  context: Record<string, unknown>;
};

export default function RatingsPage() {
  const client = useQueryClient();
  const queue = useQuery({ queryKey: ["ratings", "queue"], queryFn: () => unwrap(api.GET("/v1/ratings/queue")) });
  const rate = useMutation({
    mutationFn: (body: { item: Item; rating: Record<string, unknown> }) =>
      unwrap(
        api.POST("/v1/ratings", {
          body: {
            target_type: body.item.target_type,
            target_id: body.item.target_id,
            question_key: body.item.question_key,
            rating: body.rating,
          },
        }),
      ),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["ratings", "queue"] }),
  });
  const can = useCan("write_content");
  const items = (queue.data ?? []) as Item[];
  return (
    <>
      <PageHeader title="Ratings" description="Human judgements: ground truth for what the analyzers cannot decide." />
      <RoleNote />
      <ErrorNote error={rate.error ?? queue.error} />
      {queue.isLoading ? <Skeleton className="h-40" /> : null}
      {!queue.isLoading && items.length === 0 ? <Empty>Nothing waits for your rating.</Empty> : null}
      <div className="space-y-3">
        {items.map((item) => (
          <Card key={`${item.target_id}-${item.question_key}`}>
            <CardHeader>
              <CardTitle className="text-base">{item.prompt}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2 text-sm">
              <div className="flex flex-wrap gap-2 text-xs text-slate-600">
                <Badge variant="muted">{humanize(item.target_type)}</Badge>
                {item.context.version_id ? <span>version {String(item.context.version_id)}</span> : null}
                {item.context.automatic_verdict ? (
                  <span>automatic: {humanize(String(item.context.automatic_verdict))}</span>
                ) : null}
              </div>
              {item.target_type === "consistency" ? (
                <div className="flex gap-1" role="group" aria-label="Rating from 1 to 5">
                  {[1, 2, 3, 4, 5].map((value) => (
                    <Button
                      key={value}
                      variant="outline"
                      disabled={rate.isPending || !can.allowed}
                      title={can.reason}
                      onClick={() => rate.mutate({ item, rating: { value } })}
                    >
                      {value}
                    </Button>
                  ))}
                </div>
              ) : (
                <div className="flex gap-2">
                  <Button
                    disabled={rate.isPending || !can.allowed}
                    title={can.reason}
                    onClick={() => rate.mutate({ item, rating: { observed: true } })}
                  >
                    Yes
                  </Button>
                  <Button
                    variant="outline"
                    disabled={rate.isPending || !can.allowed}
                    title={can.reason}
                    onClick={() => rate.mutate({ item, rating: { observed: false } })}
                  >
                    No
                  </Button>
                </div>
              )}
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
