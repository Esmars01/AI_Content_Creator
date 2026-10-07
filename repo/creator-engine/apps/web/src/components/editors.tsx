"use client";
/**
 * The Advanced editors (§31): per-state performance (emotion felt/displayed and intensity,
 * strategies, attention target), behavior events, and scene intent with vocabulary dropdowns.
 * They never write the spec: each sends the operations for the fields that changed as an edit,
 * which comes back as a proposal card like an instruction does.
 */
import { useMemo, useState } from "react";

import { useStructuredEdit } from "@/components/edit-panel";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { Alert, Table, Td, Th } from "@/components/ui/misc";
import {
  actingOps,
  eventDrafts,
  eventOps,
  intentDrafts,
  intentOps,
  SCENE_INTENT_FIELDS,
  stateDrafts,
  STRATEGY_FIELDS,
  type StateDraft,
} from "@/lib/edits";
import { humanize } from "@/lib/format";
import { useVocabulary } from "@/lib/queries";
import { useCan } from "@/lib/roles";

type Json = Record<string, unknown>;

function TokenSelect({
  value,
  options,
  onChange,
  label,
  allowEmpty = false,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
  label: string;
  allowEmpty?: boolean;
}) {
  const all = value && !options.includes(value) ? [value, ...options] : options;
  return (
    <Select aria-label={label} value={value} onChange={(e) => onChange(e.target.value)} className="min-w-28">
      {allowEmpty || !value ? <option value="">—</option> : null}
      {all.map((token) => (
        <option key={token} value={token}>
          {humanize(token)}
        </option>
      ))}
    </Select>
  );
}

function Intensity({ value, onChange, label }: { value: number; onChange: (v: number) => void; label: string }) {
  return (
    <Input
      type="number"
      aria-label={label}
      min={0}
      max={1}
      step={0.05}
      className="w-20"
      value={Number.isFinite(value) ? value : 0}
      onChange={(e) => onChange(Math.min(1, Math.max(0, Number(e.target.value))))}
    />
  );
}

export function PerformanceEditor({ spec, versionId }: { spec: Json; versionId: string }) {
  const vocab = useVocabulary();
  const original = useMemo(() => stateDrafts(spec), [spec]);
  const originalEvents = useMemo(() => eventDrafts(spec), [spec]);
  const [drafts, setDrafts] = useState<StateDraft[]>(original);
  const [events, setEvents] = useState(originalEvents);
  const send = useStructuredEdit(versionId);
  const can = useCan("write_content");
  const categories = vocab.data?.categories ?? {};
  const ops = [...actingOps(original, drafts), ...eventOps(originalEvents, events)];
  const update = (key: string, change: Partial<StateDraft>) =>
    setDrafts((all) => all.map((d) => (d.key === key ? { ...d, ...change } : d)));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Performance (Advanced)</CardTitle>
        <CardDescription>Change states and events; the changes are proposed as an edit.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 overflow-x-auto">
        <Table aria-label="Acting states">
          <thead>
            <tr>
              <Th>State</Th>
              <Th>Displayed</Th>
              <Th>Felt</Th>
              {STRATEGY_FIELDS.map((f) => (
                <Th key={f}>{humanize(f)}</Th>
              ))}
              <Th>Attention</Th>
            </tr>
          </thead>
          <tbody>
            {drafts.map((d) => (
              <tr key={d.key} data-testid={`state-${d.key}`}>
                <Td className="font-mono text-xs">
                  {d.sceneKey}
                  <br />
                  {d.key}
                </Td>
                <Td>
                  <div className="flex gap-1">
                    <TokenSelect
                      label={`${d.key} displayed emotion`}
                      value={d.displayed}
                      options={categories.emotion ?? []}
                      onChange={(v) => update(d.key, { displayed: v })}
                    />
                    <Intensity
                      label={`${d.key} displayed intensity`}
                      value={d.intensity}
                      onChange={(v) => update(d.key, { intensity: v })}
                    />
                  </div>
                </Td>
                <Td>
                  <div className="flex items-center gap-1">
                    <TokenSelect
                      label={`${d.key} felt emotion`}
                      value={d.felt}
                      options={categories.emotion ?? []}
                      onChange={(v) => update(d.key, { felt: v })}
                    />
                    <Intensity
                      label={`${d.key} felt intensity`}
                      value={d.feltIntensity}
                      onChange={(v) => update(d.key, { feltIntensity: v })}
                    />
                    <label className="flex items-center gap-1 text-xs">
                      <input
                        type="checkbox"
                        checked={d.masking}
                        onChange={(e) => update(d.key, { masking: e.target.checked })}
                      />
                      masking
                    </label>
                  </div>
                </Td>
                {STRATEGY_FIELDS.map((f) => (
                  <Td key={f}>
                    <TokenSelect
                      label={`${d.key} ${f}`}
                      value={d.strategies[f] ?? ""}
                      options={categories[`strategy.${f}`] ?? []}
                      onChange={(v) => update(d.key, { strategies: { ...d.strategies, [f]: v } })}
                    />
                  </Td>
                ))}
                <Td>
                  <Input
                    aria-label={`${d.key} attention target`}
                    className="w-28"
                    value={d.attentionTarget}
                    onChange={(e) => update(d.key, { attentionTarget: e.target.value })}
                  />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
        {events.length ? (
          <Table aria-label="Behavior events">
            <thead>
              <tr>
                <Th>Event</Th>
                <Th>Type</Th>
                <Th>Intensity</Th>
                <Th>Remove</Th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.key}>
                  <Td className="font-mono text-xs">
                    {e.sceneKey} · {e.key}
                  </Td>
                  <Td>{humanize(e.type)}</Td>
                  <Td>
                    <Intensity
                      label={`${e.key} intensity`}
                      value={e.intensity}
                      onChange={(v) =>
                        setEvents((all) => all.map((x) => (x.key === e.key ? { ...x, intensity: v } : x)))
                      }
                    />
                  </Td>
                  <Td>
                    <input
                      type="checkbox"
                      aria-label={`Remove ${e.key}`}
                      checked={e.remove}
                      onChange={(ev) =>
                        setEvents((all) => all.map((x) => (x.key === e.key ? { ...x, remove: ev.target.checked } : x)))
                      }
                    />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : null}
        {send.error ? <Alert tone="danger">{send.error.message}</Alert> : null}
        <div className="flex items-center gap-2">
          <Button
            onClick={() => send.mutate(ops)}
            disabled={!ops.length || send.isPending || !can.allowed}
            title={can.reason}
            data-testid="propose-performance"
          >
            Propose {ops.length ? `${ops.length} change${ops.length > 1 ? "s" : ""}` : "changes"}
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              setDrafts(original);
              setEvents(originalEvents);
            }}
            disabled={!ops.length}
          >
            Reset
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function IntentEditor({ spec, versionId }: { spec: Json; versionId: string }) {
  const vocab = useVocabulary();
  const original = useMemo(() => intentDrafts(spec), [spec]);
  const [draft, setDraft] = useState(original);
  const send = useStructuredEdit(versionId);
  const can = useCan("write_content");
  const ops = intentOps(original, draft);
  const categories = vocab.data?.categories ?? {};
  return (
    <Card>
      <CardHeader>
        <CardTitle>Intent (Advanced)</CardTitle>
        <CardDescription>Scene intent fields; a change re-plans the acting that derives from them.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 overflow-x-auto">
        <Table aria-label="Scene intent">
          <thead>
            <tr>
              <Th>Scene</Th>
              {SCENE_INTENT_FIELDS.map((f) => (
                <Th key={f}>{humanize(f)}</Th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Object.entries(draft).map(([scene, fields]) => (
              <tr key={scene}>
                <Td className="font-mono text-xs">{scene}</Td>
                {SCENE_INTENT_FIELDS.map((f) => (
                  <Td key={f}>
                    <TokenSelect
                      label={`${scene} ${f}`}
                      value={fields[f] ?? ""}
                      options={categories[`intent.${f}`] ?? []}
                      allowEmpty
                      onChange={(v) => setDraft((d) => ({ ...d, [scene]: { ...d[scene], [f]: v } }))}
                    />
                  </Td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
        {send.error ? <Alert tone="danger">{send.error.message}</Alert> : null}
        <div>
          <Button
            onClick={() => send.mutate(ops)}
            disabled={!ops.length || send.isPending || !can.allowed}
            title={can.reason}
          >
            Propose intent changes
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
