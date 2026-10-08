"use client";
import Link from "next/link";

import { PageHeader } from "@/components/app-shell";
import { StateBadge } from "@/components/state-badge";
import { NewWorldForm } from "@/components/studio/new-identity";
import { Card, CardContent } from "@/components/ui/card";
import { Empty, LoadError, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { humanize, when } from "@/lib/format";
import { useWorlds } from "@/lib/queries";

export default function WorldsPage() {
  const worlds = useWorlds();
  return (
    <>
      <PageHeader
        title="Worlds"
        description="Recurring places with their DNA and plates. Open one for its World Studio."
      />
      <Card>
        <CardContent>
          {worlds.isLoading ? (
            <Skeleton className="h-24" />
          ) : worlds.error ? (
            <LoadError what="worlds" error={worlds.error} onRetry={() => void worlds.refetch()} />
          ) : worlds.data?.items.length ? (
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
                {worlds.data.items.map((world) => (
                  <tr key={world.id}>
                    <Td>
                      <Link className="text-blue-800 hover:underline" href={`/worlds/${world.id}`}>
                        {world.name}
                      </Link>
                    </Td>
                    <Td>{humanize(world.kind)}</Td>
                    <Td>
                      <StateBadge state={world.current_version_id ? world.status : "draft"} />
                    </Td>
                    <Td className="text-xs">{when(world.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <Empty>No worlds yet — create a world, then generate or upload its plates in its studio.</Empty>
          )}
        </CardContent>
      </Card>
      <NewWorldForm />
    </>
  );
}
