"use client";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState, type FormEvent } from "react";

import { PageHeader } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Alert, Empty, Skeleton, Table, Td, Th } from "@/components/ui/misc";
import { api, unwrap } from "@/lib/api";
import { when } from "@/lib/format";
import { keys, useProjects } from "@/lib/queries";

export default function ProjectsPage() {
  const projects = useProjects();
  const client = useQueryClient();
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: (projectName: string) =>
      unwrap(api.POST("/v1/projects", { body: { name: projectName, description: "" } })),
    onSuccess: () => {
      setName("");
      void client.invalidateQueries({ queryKey: keys.projects() });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }

  return (
    <>
      <PageHeader title="Projects" description="Videos live in projects." />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card className="col-span-2">
          <CardContent>
            {projects.isLoading ? (
              <Skeleton className="h-24" />
            ) : projects.data?.items.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Name</Th>
                    <Th>Created</Th>
                  </tr>
                </thead>
                <tbody>
                  {projects.data.items.map((project) => (
                    <tr key={project.id}>
                      <Td>
                        <Link className="text-blue-800 hover:underline" href={`/projects/${project.id}`}>
                          {project.name}
                        </Link>
                      </Td>
                      <Td className="text-xs">{when(project.created_at)}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <Empty>No projects yet — create one to start making videos.</Empty>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>New project</CardTitle>
          </CardHeader>
          <CardContent>
            <form className="flex flex-col gap-2" onSubmit={submit}>
              <Label htmlFor="project-name">Name</Label>
              <Input id="project-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
              {create.error ? <Alert tone="danger">{create.error.message}</Alert> : null}
              <Button type="submit" disabled={!name.trim() || create.isPending}>
                Create project
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
