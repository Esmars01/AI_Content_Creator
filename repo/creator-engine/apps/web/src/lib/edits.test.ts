import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  actingOps,
  coverageChanges,
  describeOp,
  eventDrafts,
  eventOps,
  flatten,
  groupDiff,
  intentDrafts,
  intentOps,
  lockLabel,
  locksBody,
  locksOf,
  readablePath,
  short,
  stateDrafts,
  summarizeImpact,
  versionTree,
  type VersionSummary,
} from "./edits";

type Json = Record<string, unknown>;

function first<T>(items: readonly T[]): T {
  const [item] = items;
  if (item === undefined) throw new Error("empty");
  return item;
}

const schema = JSON.parse(
  readFileSync(resolve(__dirname, "../../../../packages/ts/api-client/schema/edit_operations.schema.json"), "utf8"),
) as { $defs: Record<string, { properties?: Json }> };

const SPEC: Json = {
  scenes: [
    {
      key: "scn_hook",
      intent: { narrative_goal: "challenge_belief", emotional_goal: null },
      acting: {
        states: [
          {
            key: "st_1",
            emotion: {
              felt: { label: "amused", intensity: 0.4 },
              displayed: { label: "confident", intensity: 0.6 },
              masking: true,
            },
            strategies: {
              prosody: "assertive_light",
              gaze: "hold_camera",
              gesture: "illustrative_light",
              posture: "seated_upright",
              reaction: "suppressed",
              camera_awareness: "direct_address",
            },
            attention_target: "camera",
          },
        ],
        events: [{ key: "ev_1", type: "small_smile", intensity: 0.4 }],
      },
    },
  ],
  locks: [
    { group: "voice", scope: { character_keys: ["char_alex"], scene_keys: null, shot_keys: null }, set_by: "user" },
  ],
};

describe("proposal display", () => {
  it("summarizes the impact, including no-visible-effect, locks, cost and issues", () => {
    const impact = summarizeImpact({
      regenerate: ["behavior.resolve:scn_reveal", "avatar.render:sht_4:c1:t1"],
      cascade: ["mix.audio:main"],
      keep: ["tts.segment:seg_1", "tts.segment:seg_2"],
      no_visible_effect: [
        { node_key: "post.expression:sht_4", reason: "the change leaves the compiled output unchanged" },
      ],
      generation: ["avatar.render:sht_4:c1:t1"],
      locks_blocking: [{ group: "voice", reason: "delivered audio kept for unchanged text (seg_2)" }],
      estimate: { usd: 0.12, gpu_s: 30 },
      issues: [{ code: "locked", message: "the `camera` lock", path: "/scenes[scn_hook]/shots[sht_1]/camera" }],
      planner: "fixture",
    });
    expect(impact.regenerate).toHaveLength(2);
    expect(impact.keep).toBe(2);
    expect(impact.noVisibleEffect[0]).toEqual({
      key: "post.expression:sht_4",
      reason: "the change leaves the compiled output unchanged",
    });
    expect(first(impact.locksBlocking).label).toBe("voice");
    expect([impact.estimateUsd, impact.gpuSeconds]).toEqual([0.12, 30]);
    expect(first(impact.issues).code).toBe("locked");
    expect(summarizeImpact(null).regenerate).toEqual([]);
  });

  it("groups diffs by area and renders values briefly", () => {
    const groups = groupDiff([
      {
        path: "/scenes[scn_reveal]/acting/states[st_2]/strategies/gaze",
        kind: "changed",
        area: "acting",
        before: "a",
        after: "b",
      },
      {
        path: "/scenes[scn_reveal]/shots[sht_4]/camera/moves[mv_9]",
        kind: "added",
        area: "camera",
        after: { type: "handheld_drift" },
      },
      {
        path: "/scenes[scn_reveal]/acting/events[ev_9]",
        kind: "added",
        area: "acting",
        after: { type: "eyebrow_raise" },
      },
    ]);
    expect(groups.map(([area, rows]) => [area, rows.length])).toEqual([
      ["acting", 2],
      ["camera", 1],
    ]);
    expect(short({ label: "skeptical", intensity: 0.8 })).toBe("skeptical (0.8)");
    expect(short({ type: "handheld_drift" })).toBe("handheld_drift");
    expect(short(null)).toBe("—");
    expect(readablePath("/scenes[scn_reveal]/acting/states[st_2]/strategies/gaze")).toBe(
      "scn_reveal · st_2 · strategies.gaze",
    );
    expect(readablePath("/scenes[scn_reveal]/acting/states[st_2]/emotion/felt/label")).toBe(
      "scn_reveal · st_2 · felt.label",
    );
    expect(readablePath("/intent/video/emotional_arc")).toBe("video.emotional_arc");
    expect(readablePath("/scenes[scn_reveal]/acting/events[ev_3]")).toBe("scn_reveal · ev_3");
  });

  it("reads the coverage delta with locks and no-visible-effect flags", () => {
    const rows = coverageChanges({
      items: [
        {
          item_ref: "/scenes[scn_reveal]/acting/states[st_2]/strategies/prosody",
          dimension: "prosody_rate",
          before: { level: "HONORED", method: "native_parametric" },
          after: { level: "HONORED", method: "native_parametric" },
          change: "request_changed",
          blocked_by_lock: "voice",
          reason: "the voice lock keeps the delivered audio",
        },
      ],
    });
    expect(rows[0]).toMatchObject({
      blockedByLock: "voice",
      before: "HONORED · Native parametric",
      change: "request_changed",
    });
  });

  it("describes operations in plain words", () => {
    expect(
      describeOp({
        op: "set_acting",
        scope: { scene_keys: ["scn_reveal"] },
        changes: {
          emotion: { displayed: { label: "skeptical", intensity_delta: 0.2 } },
          strategies: { gaze: "side_glance" },
        },
      }),
    ).toBe("Acting in scn_reveal: displayed skeptical, intensity +0.2, Gaze → Side glance");
    expect(describeOp({ op: "set_camera", add_moves: [{ type: "handheld_drift" }] })).toBe(
      "Camera: add Handheld drift",
    );
    expect(describeOp({ op: "regenerate", components: ["avatar_video"], seed_policy: "new" })).toBe(
      "Regenerate Avatar video (new seed)",
    );
    expect(describeOp({ op: "set_lock", add: [{ group: "appearance" }], remove: [{ group: "voice" }] })).toBe(
      "Locks: +appearance −voice",
    );
  });
});

describe("structured editors", () => {
  it("emits set_acting only for changed fields, with keys the schema accepts", () => {
    const original = stateDrafts(SPEC);
    expect(actingOps(original, original)).toEqual([]);
    const drafts = original.map((d) => ({
      ...d,
      displayed: "skeptical",
      intensity: 0.75,
      strategies: { ...d.strategies, gaze: "side_glance" },
    }));
    const op = first(actingOps(original, drafts));
    expect(op).toEqual({
      op: "set_acting",
      scope: { scene_keys: ["scn_hook"], state_keys: ["st_1"] },
      changes: { emotion: { displayed: { label: "skeptical", intensity: 0.75 } }, strategies: { gaze: "side_glance" } },
      reason: "edited in the performance editor",
    });
    const acting = schema.$defs.ActingChanges?.properties ?? {};
    for (const key of Object.keys(op.changes as Json)) expect(acting).toHaveProperty(key);
    const scope = schema.$defs.EditScope?.properties ?? {};
    for (const key of Object.keys(op.scope as Json)) expect(scope).toHaveProperty(key);
  });

  it("emits event removals and intensity changes with their scene", () => {
    const original = eventDrafts(SPEC);
    const event = first(original);
    expect(eventOps(original, [{ ...event, intensity: 0.7 }])).toEqual([
      {
        op: "set_behavior_event",
        scene_key: "scn_hook",
        event_key: "ev_1",
        changes: { intensity: 0.7 },
        reason: "edited in the events editor",
      },
    ]);
    expect(eventOps(original, [{ ...event, remove: true }])[0]).toMatchObject({
      op: "remove_behavior_event",
      scene_key: "scn_hook",
      event_key: "ev_1",
    });
    for (const name of ["SetBehaviorEvent", "RemoveBehaviorEvent", "SetIntent"])
      expect(schema.$defs).toHaveProperty(name);
  });

  it("emits set_intent per scene for changed fields (empty clears)", () => {
    const original = intentDrafts(SPEC);
    const draft = { scn_hook: { ...original.scn_hook, narrative_goal: "", emotional_goal: "curiosity" } };
    expect(intentOps(original, draft)).toEqual([
      {
        op: "set_intent",
        scope: { scene_keys: ["scn_hook"] },
        fields: { narrative_goal: null, emotional_goal: "curiosity" },
        reason: "edited in the intent editor",
      },
    ]);
  });
});

describe("locks and versions", () => {
  it("reads locks and builds the PUT body", () => {
    const rows = locksOf(SPEC);
    expect(lockLabel(first(rows))).toBe("Voice (char_alex)");
    expect(locksBody(rows)).toEqual({
      locks: [{ group: "voice", scope: { scene_keys: null, character_keys: ["char_alex"], shot_keys: null } }],
    });
    expect(
      lockLabel({ group: "world", scope: { scene_keys: null, character_keys: null, shot_keys: null }, setBy: "user" }),
    ).toBe("World (whole video)");
  });

  it("builds the version tree by parent, duplicates being roots", () => {
    const v = (id: string, number: number, parent: string | null, origin = "edit"): VersionSummary =>
      ({ id, number, parent_version_id: parent, origin, branch: "main", state: "ready" }) as VersionSummary;
    const tree = versionTree([
      v("c", 3, "a", "restore"),
      v("a", 1, null, "plan"),
      v("b", 2, "a"),
      v("d", 4, "b"),
      v("x", 1, "elsewhere", "duplicate"),
    ]);
    expect(flatten(tree).map((n) => [n.version.id, n.depth])).toEqual([
      ["a", 0],
      ["b", 1],
      ["d", 2],
      ["c", 1],
      ["x", 0],
    ]);
  });
});
