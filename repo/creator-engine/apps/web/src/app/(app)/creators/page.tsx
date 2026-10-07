"use client";
import Link from "next/link";

import { PageHeader } from "@/components/app-shell";
import { StateBadge } from "@/components/state-badge";
import { NewCreatorForm } from "@/components/studio/new-identity";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, LoadError, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { humanize, when } from "@/lib/format";
import { useCreators } from "@/lib/queries";

export default function CreatorsPage() {
  const creators = useCreators();
  return (
    <>
      <PageHeader
        title="Creators"
        description="Synthetic creators with their DNA, voices, wardrobe and memory. Open one for its Creator Studio."
      />
      <Card>
        <CardContent>
          {creators.isLoading ? (
            <Skeleton className="h-24" />
          ) : creators.error ? (
            <LoadError what="creators" error={creators.error} onRetry={() => void creators.refetch()} />
          ) : creators.data?.items.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Kind</Th>
                  <Th>Status</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {creators.data.items.map((creator) => (
                  <tr key={creator.id}>
                    <Td>
                      <Link className="text-blue-800 hover:underline" href={`/creators/${creator.id}`}>
                        {creator.name}
                      </Link>
                    </Td>
                    <Td>{humanize(creator.kind)}</Td>
                    <Td>
                      <StateBadge state={creator.current_version_id ? creator.status : "draft"} />
                    </Td>
                    <Td className="text-xs">{when(creator.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>No creators.</Empty>
          )}
        </CardContent>
      </Card>
      <NewCreatorForm />
    </>
  );
}
