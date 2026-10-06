"""The Creative Director critique (§26): scores and findings with proposed edit operations.

`critique(...)` reads what the system measured about a rendered version — the coverage report,
the shot and take QC reports, world QC, render QC, consistency reports and a VLM pass over the
proxy — and writes `Critique{scores, findings}`:

- every score is 0..1 with its `basis`, or `None` with the reason when nothing measured it (a
  missing measurement is never scored);
- every finding names its evidence (a time range, coverage item refs, the shot) and proposes typed
  `EditOperation`s (§28) that `POST /v1/critiques/{id}/findings/{n}:propose` turns into an edit
  proposal — nothing is applied without the user (§26).

The critic is deterministic (`CRITIC_VERSION`): rules over measurements. A language-model critic
over the same evidence is a later refinement; the evidence and the operation vocabulary stay.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["CRITIC_VERSION", "CRITIQUE_SCHEMA", "SCORE_KEYS", "critique"]

CRITIC_VERSION = "rules-v1"
SCORE_KEYS = (
    "hook",
    "pacing",
    "emotional_variation",
    "behavior_believability",
    "character_consistency",
    "world_continuity",
    "realism",
    "visual_quality",
    "storytelling",
    "clarity",
    "cta",
    "scene_variety",
    "audio",
)
FAILED_OBSERVATION = ("NOT_OBSERVED", "CONTRADICTED")

# The VLM pass over the proxy (2 fps): perceptual scores and time-stamped problems.
CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["scores", "problems"],
    "properties": {
        "scores": {
            "type": "object",
            "properties": {k: {"type": "number", "minimum": 0, "maximum": 1} for k in ("realism", "visual_quality")},
        },
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["t_s", "issue", "severity"],
                "properties": {
                    "t_s": {"type": "number", "minimum": 0},
                    "issue": {"type": "string", "maxLength": 300},
                    "severity": {"type": "string", "enum": ["critical", "minor"]},
                },
            },
        },
    },
}


def _score(value: float | None, basis: str) -> dict[str, Any]:
    if value is None:
        return {"score": None, "basis": basis}
    return {"score": round(max(0.0, min(1.0, value)), 3), "basis": basis}


def _shot_at(t: float, shot_times: Mapping[str, tuple[float, float]]) -> str | None:
    for key, (a, b) in shot_times.items():
        if a <= t < b:
            return key
    return None


def _regenerate(shot_key: str, component: str, *, strategy: str = "full", takes: int | None = None) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "regenerate",
        "scope": {"shot_keys": [shot_key]},
        "components": [component],
        "seed_policy": "new",
        "strategy": strategy,
    }
    if takes:
        op["takes"] = takes
    return op


def critique(
    *,
    spec: Mapping[str, Any],
    coverage_entries: Sequence[Mapping[str, Any]] = (),
    coverage_targets: Mapping[str, str] | None = None,
    shot_reports: Sequence[Mapping[str, Any]] = (),
    take_reports: Sequence[Mapping[str, Any]] = (),
    vlm: Mapping[str, Any] | None = None,
    world_scores: Sequence[float] = (),
    render_checks: Mapping[str, bool] | None = None,
    consistency_verdicts: Sequence[str] = (),
    speech_passed: Sequence[bool] = (),
    shot_times: Mapping[str, tuple[float, float]] | None = None,
    max_shot_s: float | None = None,
) -> dict[str, Any]:
    shot_times = dict(shot_times or {})
    scenes = list(spec.get("scenes", []))
    shots = [s for sc in scenes for s in sc.get("shots", [])]
    kind_of = {s["key"]: ("broll" if s.get("type") not in ("talking_head", None) else "avatar_video") for s in shots}
    findings: list[dict[str, Any]] = []

    def add(category: str, issue: str, impact: str, ops: list[dict[str, Any]], **evidence: Any) -> None:
        findings.append(
            {
                "id": f"f{len(findings) + 1}",
                "category": category,
                "issue": issue,
                "evidence": {k: v for k, v in evidence.items() if v not in (None, [], {})},
                "proposed_ops": ops,
                "impact": impact,
                "estimate": {
                    "components": sorted({c for o in ops for c in o.get("components", [])}),
                    "note": "the exact cost is computed when the finding is proposed as an edit",
                },
            }
        )

    # QC: shots the ladder could not fix
    for report in shot_reports:
        if report.get("verdict") != "fail":
            continue
        shot = str(report.get("shot_key"))
        failures = [f for h in report.get("ladder", []) for f in h.get("failures", [])]
        checks = sorted({str(f.get("check")) for f in failures})
        lipsync_only = checks == ["qc.lipsync"]
        add(
            "visual_quality",
            f"shot {shot} still fails QC after the retry ladder ({', '.join(checks) or 'see the QC report'})",
            "high",
            [
                _regenerate(
                    shot,
                    kind_of.get(shot, "avatar_video"),
                    strategy="lipsync_patch" if lipsync_only else "full",
                    takes=2,
                )
            ],
            shot_key=shot,
            time_range=list(shot_times.get(shot, ())) or None,
        )
    # Behavior: requested and not seen
    targets = dict(coverage_targets or {})
    delivered = measurable = 0
    for entry in coverage_entries:
        outcome = str(entry.get("outcome") or "")
        verdict = outcome.rsplit("_", 1)[-1] if outcome else ""
        if outcome.endswith(("CONFIRMED", "PARTIAL", "NOT_OBSERVED", "CONTRADICTED")):
            measurable += 1
            delivered += outcome.endswith("CONFIRMED")
        if entry.get("expected_for_method") and entry.get("approximation_executed") is False:
            target = targets.get(f"{entry.get('item_ref')}|{entry.get('dimension')}", "")
            target_shot: str | None = target.split(":c")[0] if ":c" in target else None
            add(
                "behavior_believability",
                f"the editorial approximation of {entry.get('dimension')} was planned but not executed",
                "medium",
                [_regenerate(target_shot, "camera_post")] if target_shot else [],
                item_refs=[entry.get("item_ref")],
                shot_key=target_shot,
            )
            continue
        if not any(outcome.endswith(v) for v in FAILED_OBSERVATION) or entry.get("expected_for_method"):
            continue
        target = targets.get(f"{entry.get('item_ref')}|{entry.get('dimension')}", "")
        target_shot = target.split(":c")[0] if ":c" in target else None
        add(
            "behavior_believability",
            f"{entry.get('dimension')} was requested ({entry.get('requested') or entry.get('item_ref')}) but "
            f"{'the opposite was' if verdict == 'CONTRADICTED' else 'it was not'} observed",
            "medium",
            [_regenerate(target_shot, "avatar_video", takes=2)] if target_shot else [],
            item_refs=[entry.get("item_ref")],
            shot_key=target_shot,
        )
    # The VLM pass: time-stamped problems
    answer = dict(vlm or {})
    for problem in answer.get("problems", []) or []:
        try:
            t = float(problem.get("t_s", 0.0))
        except (TypeError, ValueError):
            continue
        at = _shot_at(t, shot_times)
        if problem.get("severity") != "critical" or at is None:
            continue
        add(
            "realism",
            f"visible problem at {t:.1f} s: {str(problem.get('issue', ''))[:200]}",
            "high",
            [_regenerate(at, kind_of.get(at, "avatar_video"))],
            time_range=[round(t, 2), round(t + 0.5, 2)],
            shot_key=at,
            source="vlm" + (" (mock)" if answer.get("mock") else ""),
        )
    # Pacing: shots longer than the mode allows
    long_shots = [k for k, (a, b) in shot_times.items() if max_shot_s and b - a > max_shot_s + 0.5]
    for shot in long_shots:
        scene = next((sc["key"] for sc in scenes if any(s["key"] == shot for s in sc.get("shots", []))), None)
        if scene:
            a, b = shot_times[shot]
            add(
                "pacing",
                f"shot {shot} runs {b - a:.1f} s; the mode's grammar keeps shots under {max_shot_s:.0f} s",
                "low",
                [{"op": "set_pacing", "scope": {"scene_keys": [scene]}, "cut_cadence": "fast"}],
                time_range=[round(a, 2), round(b, 2)],
                shot_key=shot,
            )

    durations = [b - a for a, b in shot_times.values()]
    first_scene = scenes[0] if scenes else {}
    first_shots = [shot_times[s["key"]] for s in first_scene.get("shots", []) if s["key"] in shot_times]
    hook_s = max(b for _, b in first_shots) if first_shots else None
    emotions = {
        str(st.get("label") or st.get("emotion"))
        for sc in scenes
        for st in dict(sc.get("acting") or {}).get("states", [])
        if st.get("label") or st.get("emotion")
    }
    shot_types = {str(s.get("type")) for s in shots}
    has_cta = any(
        "cta" in str(sc.get("key", "")) or "cta" in str(dict(sc.get("intent") or {}).get("goal", "")) for sc in scenes
    )
    vlm_scores = dict(answer.get("scores") or {})
    vqa = [
        bool(v.get("passed")) for r in take_reports for k, v in dict(r.get("verdicts") or {}).items() if k == "qc.vqa"
    ]
    consistency = {"in_band": 1.0, "warn": 0.6, "out_of_band": 0.3}
    mock_note = " (mock VLM)" if answer.get("mock") else ""
    scores = {
        "hook": _score(
            None if hook_s is None else 1.0 - max(0.0, hook_s - 3.0) / 6.0,
            f"first scene lasts {hook_s:.1f} s (≤ 3 s scores 1)" if hook_s is not None else "no timing",
        ),
        "pacing": _score(
            None if not durations or not max_shot_s else sum(d <= max_shot_s + 0.5 for d in durations) / len(durations),
            f"share of shots within the mode's {max_shot_s} s" if max_shot_s else "the mode sets no shot length",
        ),
        "emotional_variation": _score(
            min(1.0, len(emotions) / 3.0) if scenes else None, f"{len(emotions)} distinct requested states"
        ),
        "behavior_believability": _score(
            delivered / measurable if measurable else None,
            f"{delivered}/{measurable} measurable behavior items confirmed",
        ),
        "character_consistency": _score(
            statistics.fmean(consistency[v] for v in consistency_verdicts) if consistency_verdicts else None,
            "consistency reports" if consistency_verdicts else "no consistency report yet",
        ),
        "world_continuity": _score(
            statistics.median(world_scores) if world_scores else None, "median environment identity of the shots"
        ),
        "realism": _score(vlm_scores.get("realism"), "VLM pass over the proxy" + mock_note),
        "visual_quality": _score(
            vlm_scores.get("visual_quality", (sum(vqa) / len(vqa)) if vqa else None),
            ("VLM pass over the proxy" + mock_note) if "visual_quality" in vlm_scores else "share of takes passing VQA",
        ),
        "storytelling": _score(
            min(1.0, len(scenes) / 3.0) if scenes else None,
            f"{len(scenes)} scenes (structure only; story quality needs a human)",
        ),
        "clarity": _score(
            sum(speech_passed) / len(speech_passed) if speech_passed else None,
            "share of segments that passed the exact-script check",
        ),
        "cta": _score(
            1.0 if has_cta else 0.0 if scenes else None,
            "a call-to-action scene is present" if has_cta else "no call-to-action scene",
        ),
        "scene_variety": _score(min(1.0, len(shot_types) / 3.0) if shots else None, f"{len(shot_types)} shot types"),
        "audio": _score(
            sum(bool(v) for v in render_checks.values()) / len(render_checks) if render_checks else None,
            "render checks (loudness, true peak, audio present)",
        ),
    }
    if scores["cta"]["score"] == 0.0 and scenes:
        findings.append(
            {
                "id": f"f{len(findings) + 1}",
                "category": "cta",
                "issue": "the video has no call-to-action scene",
                "evidence": {},
                "proposed_ops": [],
                "impact": "low",
                "estimate": {"components": [], "note": "needs a script change: ask the Director for a CTA scene"},
            }
        )
    return {"critic": CRITIC_VERSION, "scores": scores, "findings": findings}
