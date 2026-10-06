"use client";
/** Blind pairwise rating of a benchmark (§24, Phase 11): two outputs of the same case and seed — the
 * candidate and the production default — in an order decided per pair and rater, so the candidate
 * cannot be told by its side. Each choice re-decides the benchmark. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { use } from "react";

import { PageHeader } from "@/components/app-shell";
import { ErrorNote } from "@/components/studio/common";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, Skeleton } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { mediaKind } from "@/lib/qc";

function Media({ url, mime, label }: { url: string; mime: string; label: string }) {
  const kind = mediaKind(mime);
  if (kind === "video") return <video controls src={url} aria-label={label} className="max-h-80 w-full rounded" />;
  if (kind === "audio") return <audio controls src={url} aria-label={label} className="w-full" />;
  if (kind === "image") return <img src={url} alt={label} className="max-h-80 rounded" />;
  return (
    <a className="underline" href={url}>
      {label}
    </a>
  );
}

export default function BenchmarkPairsPage({ params }: { params: Promise<{ benchmarkId: string }> }) {
  const { benchmarkId } = use(params);
  const client = useQueryClient();
  const bench = useQuery({
    queryKey: ["benchmark", benchmarkId],
    queryFn: () =>
      unwrap(api.GET("/v1/admin/benchmarks/{benchmark_id}", { params: { path: { benchmark_id: benchmarkId } } })),
  });
  const pairs = useQuery({
    queryKey: ["benchmark", benchmarkId, "pairs"],
    queryFn: () =>
      unwrap(api.GET("/v1/admin/benchmarks/{benchmark_id}/pairs", { params: { path: { benchmark_id: benchmarkId } } })),
  });
  const rate = useMutation({
    mutationFn: ({ pairId, preferred }: { pairId: string; preferred: "left" | "right" | "tie" }) =>
      unwrap(
        api.POST("/v1/admin/benchmarks/{benchmark_id}/pairs/{pair_id}:rate", {
          params: { path: { benchmark_id: benchmarkId, pair_id: pairId } },
          body: { preferred, note: "" },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["benchmark", benchmarkId] });
    },
  });
  const decided = bench.data && bench.data.verdict !== "pending";
  return (
    <>
      <PageHeader title="Rate benchmark pairs" description="Which output is better? The sides are shuffled for you." />
      <ErrorNote error={rate.error ?? pairs.error} />
      {bench.data ? (
        <p className="mb-2 text-sm">
          Verdict:{" "}
          <Badge
            variant={bench.data.verdict === "pass" ? "success" : bench.data.verdict === "fail" ? "danger" : "info"}
          >
            {bench.data.verdict}
          </Badge>
        </p>
      ) : null}
      {pairs.isLoading ? <Skeleton className="h-64" /> : null}
      {pairs.data?.length === 0 ? <Empty>This benchmark has no pairs to rate.</Empty> : null}
      <div className="space-y-3">
        {(pairs.data ?? []).map((pair) => (
          <Card key={pair.id}>
            <CardHeader>
              <CardTitle className="text-base">
                {pair.item_key} {pair.rated ? <Badge variant="success">rated</Badge> : null}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="grid grid-cols-2 gap-3">
                <Media url={pair.left_url} mime={pair.left_mime} label="Left output" />
                <Media url={pair.right_url} mime={pair.right_mime} label="Right output" />
              </div>
              <div className="mt-2 flex gap-2">
                {(["left", "tie", "right"] as const).map((choice) => (
                  <Button
                    key={choice}
                    variant={choice === "tie" ? "outline" : "default"}
                    disabled={rate.isPending || Boolean(decided)}
                    onClick={() => rate.mutate({ pairId: pair.id, preferred: choice })}
                  >
                    {choice === "tie" ? "About the same" : choice === "left" ? "Left is better" : "Right is better"}
                  </Button>
                ))}
              </div>
            </CardContent>
          </Card>
        ))}
      </div>
    </>
  );
}
