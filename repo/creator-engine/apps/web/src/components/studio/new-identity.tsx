"use client";
/** "New creator" and "New world" (audit CR-CREATE): the API creates both for any writer, but the
 * Creators and Worlds pages were lists only — nothing in the app could start one. A creator starts
 * as a draft with a display name and bio (or another creator's DNA); a world starts from an existing
 * world's DNA under a new name. Both then open in their Studio, where drafts are edited and approved. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { ErrorNote } from "@/components/studio/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { api, unwrap } from "@/lib/api";
import { keys, useCreators, useVocabulary, useWorlds } from "@/lib/queries";
import { useCan } from "@/lib/roles";

export function NewCreatorForm() {
  const router = useRouter();
  const client = useQueryClient();
  const can = useCan("write_content");
  const vocab = useVocabulary();
  const creators = useCreators();
  const [name, setName] = useState("");
  const [bio, setBio] = useState("");
  const [from, setFrom] = useState("");
  const create = useMutation({
    mutationFn: async () => {
      let dna: Record<string, unknown> = { vocab_version: vocab.data?.version, identity: {} };
      if (from) {
        const source = await unwrap(api.GET("/v1/creators/{creator_id}", { params: { path: { creator_id: from } } }));
        const baseId = source.current_version_id ?? source.versions?.[source.versions.length - 1]?.id;
        if (baseId) {
          const base = await unwrap(
            api.GET("/v1/creator-versions/{creator_version_id}", { params: { path: { creator_version_id: baseId } } }),
          );
          dna = { ...(base.dna as Record<string, unknown>) };
        }
      }
      const identity: Record<string, unknown> = { canon: [], ...((dna.identity as Record<string, unknown>) ?? {}) };
      dna = { ...dna, identity: { ...identity, display_name: name.trim(), bio: bio.trim() || identity.bio || "" } };
      return unwrap(api.POST("/v1/creators", { body: { name: name.trim(), kind: "synthetic", dna: dna as never } }));
    },
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: ["creators"] });
      router.push(`/creators/${created.id}`);
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>New creator</CardTitle>
        <CardDescription>
          A synthetic creator starts as a draft. Give it a look, a voice and outfits in its Creator Studio, link them in
          Creator DNA, then approve it.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && can.allowed) create.mutate();
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-creator-name">Name</Label>
            <Input id="new-creator-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-creator-from">Start from</Label>
            <Select id="new-creator-from" value={from} onChange={(e) => setFrom(e.target.value)}>
              <option value="">A blank DNA</option>
              {(creators.data?.items ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}&apos;s DNA
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1 md:col-span-2">
            <Label htmlFor="new-creator-bio">Bio</Label>
            <Textarea id="new-creator-bio" rows={2} value={bio} onChange={(e) => setBio(e.target.value)} />
          </div>
          <div className="md:col-span-2">
            <Button
              type="submit"
              disabled={!name.trim() || !vocab.data || create.isPending || !can.allowed}
              title={can.reason}
            >
              {create.isPending ? "Creating…" : "Create creator"}
            </Button>
          </div>
          <div className="md:col-span-2">
            <ErrorNote error={create.error} />
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

export function NewWorldForm() {
  const router = useRouter();
  const client = useQueryClient();
  const can = useCan("write_content");
  const worlds = useWorlds();
  const sources = (worlds.data?.items ?? []).filter((w) => w.current_version_id);
  const [name, setName] = useState("");
  const [from, setFrom] = useState("");
  const source = from || sources[0]?.id || "";
  const create = useMutation({
    mutationFn: async () => {
      const world = sources.find((w) => w.id === source);
      if (!world?.current_version_id) throw new Error("Choose a world to start from.");
      const version = await unwrap(
        api.GET("/v1/world-versions/{world_version_id}", {
          params: { path: { world_version_id: world.current_version_id } },
        }),
      );
      const dna: Record<string, unknown> = { ...(version.dna as Record<string, unknown>), name: name.trim() };
      delete dna.fingerprints_artifact_id; // computed for the source's plates, not the copy's
      return unwrap(api.POST("/v1/worlds", { body: { name: name.trim(), dna: dna as never } }));
    },
    onSuccess: async (created) => {
      await client.invalidateQueries({ queryKey: keys.worlds() });
      router.push(`/worlds/${created.id}`);
    },
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle>New world</CardTitle>
        <CardDescription>
          A new world starts as a draft copy of an existing world&apos;s DNA. Change it in the World Studio, generate
          and choose plates, then approve it.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="grid gap-3 md:grid-cols-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim() && can.allowed) create.mutate();
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-world-name">Name</Label>
            <Input id="new-world-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="new-world-from">Start from</Label>
            <Select id="new-world-from" value={source} onChange={(e) => setFrom(e.target.value)}>
              {sources.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="md:col-span-2">
            <Button
              type="submit"
              disabled={!name.trim() || !source || create.isPending || !can.allowed}
              title={can.reason}
            >
              {create.isPending ? "Creating…" : "Create world"}
            </Button>
          </div>
          <div className="md:col-span-2">
            <ErrorNote error={create.error} />
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
