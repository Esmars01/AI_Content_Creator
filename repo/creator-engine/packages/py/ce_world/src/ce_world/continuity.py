"""World continuity checks beyond the CPU statistics (§19.6, Phase 11): VLM element checks and
background continuity across a video's shots.

- `element_expectations`: what a shot from a camera position should show — the layout's visible
  elements (signature ones first), their expected states and left/right relations. Relations are
  projected from the floor plan (x, y in 0..1) onto the camera's right vector (position → target);
  a position without coordinates has no relations to check.
- `element_question` / `score_elements`: one structured VLM question per shot (a JSON schema whose
  enum lists the element keys) and its score: presence, relations and states, each counted only
  where the answer says something. These checks start `reliability: low` and warn until calibrated
  against human ratings (§19.6).
- `background_continuity`: adjacent shots of the same binding (world version × camera position ×
  time of day): background embedding cosine and colour-statistics agreement.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["background_continuity", "element_expectations", "element_question", "score_elements"]


def _right_vector(camera: Mapping[str, Any]) -> tuple[float, float] | None:
    pos, target = camera.get("position"), camera.get("target")
    if not pos or not target:
        return None
    fx, fy = float(target[0]) - float(pos[0]), float(target[1]) - float(pos[1])
    norm = math.hypot(fx, fy)
    if norm < 1e-9:
        return None
    # z is up; looking along (fx, fy) on the floor plan, screen-right is forward × up
    return fy / norm, -fx / norm


def element_expectations(dna: Mapping[str, Any], camera_position_key: str | None) -> dict[str, Any]:
    elements = {str(e["key"]): e for e in dna.get("elements", [])}
    layout = dict(dict(dna.get("background_layouts") or {}).get(camera_position_key or "", {}) or {})
    visible = [k for k in layout.get("visible_elements", []) if k in elements]
    must = list(dict(dict(dna.get("continuity") or {}).get("must_show_from") or {}).get(camera_position_key or "", []))
    keys = list(dict.fromkeys([*must, *sorted(visible, key=lambda k: not elements[k].get("signature")), *visible]))
    expected = [
        {
            "key": k,
            "label": str(elements[k].get("label") or elements[k].get("kind") or k),
            "signature": bool(elements[k].get("signature")),
            "must_show": k in must,
            "state": elements[k].get("default_state"),
        }
        for k in keys
        if k in elements
    ]
    camera: Mapping[str, Any] = next(
        (c for c in dna.get("camera_positions", []) if c.get("key") == camera_position_key), {}
    )
    right = _right_vector(camera)
    relations: list[dict[str, str]] = []
    if right is not None and len(expected) > 1:
        placed = sorted(
            expected,
            key=lambda e: (
                float(elements[e["key"]]["position"][0]) * right[0]
                + float(elements[e["key"]]["position"][1]) * right[1]
            ),
        )
        for a, b in itertools.pairwise(placed):
            relations.append({"left": a["key"], "right": b["key"]})
    return {"elements": expected, "relations": relations}


def element_question(expectations: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    elements = list(expectations.get("elements", []))
    keys = [e["key"] for e in elements]
    lines = "\n".join(
        f"- {e['key']}: {e['label']}" + (f" (should be {e['state']})" if e.get("state") else "") for e in elements
    )
    relations = "\n".join(f"- {r['left']} is left of {r['right']}" for r in expectations.get("relations", []))
    question = (
        "This is a frame of a video set in a known room. For each listed element, say whether it is visible, "
        "and whether its state matches when a state is given.\n"
        f"{lines}\n"
        + (f"Then check these left/right relations as seen in the frame:\n{relations}\n" if relations else "")
        + "Answer with JSON that matches the schema; use null when you cannot tell."
    )
    schema = {
        "type": "object",
        "required": ["elements"],
        "properties": {
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["key", "present"],
                    "properties": {
                        "key": {"type": "string", "enum": keys},
                        "present": {"type": ["boolean", "null"]},
                        "state_ok": {"type": ["boolean", "null"]},
                    },
                },
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["left", "right", "holds"],
                    "properties": {
                        "left": {"type": "string", "enum": keys},
                        "right": {"type": "string", "enum": keys},
                        "holds": {"type": ["boolean", "null"]},
                    },
                },
            },
        },
    }
    return question, schema


def score_elements(answer: Mapping[str, Any], expectations: Mapping[str, Any]) -> dict[str, Any]:
    """Share of positive answers over the answered questions; must-show elements that are missing
    are listed. `score` is None when nothing was answered."""
    expected = {e["key"]: e for e in expectations.get("elements", [])}
    answers = {
        str(a.get("key")): a
        for a in answer.get("elements", []) or []
        if isinstance(a, Mapping) and a.get("key") in expected
    }
    marks: list[bool] = []
    missing: list[str] = []
    states: list[bool] = []
    for key, element in expected.items():
        present = answers.get(key, {}).get("present")
        if isinstance(present, bool):
            marks.append(present)
            if not present and (element.get("must_show") or element.get("signature")):
                missing.append(key)
        state_ok = answers.get(key, {}).get("state_ok")
        if element.get("state") and isinstance(state_ok, bool):
            states.append(state_ok)
    wanted = {(r["left"], r["right"]) for r in expectations.get("relations", [])}
    relations = [
        bool(r.get("holds"))
        for r in answer.get("relations", []) or []
        if isinstance(r, Mapping) and (r.get("left"), r.get("right")) in wanted and isinstance(r.get("holds"), bool)
    ]
    answered = marks + states + relations
    return {
        "score": round(sum(answered) / len(answered), 4) if answered else None,
        "present": f"{sum(marks)}/{len(marks)}",
        "states_ok": f"{sum(states)}/{len(states)}" if states else None,
        "relations_ok": f"{sum(relations)}/{len(relations)}" if relations else None,
        "missing": missing,
    }


def _cos(a: Sequence[float], b: Sequence[float]) -> float | None:
    if not a or len(a) != len(b):
        return None
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    if na < 1e-12 or nb < 1e-12:
        return None
    return sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb)


def background_continuity(shots: Sequence[Mapping[str, Any]], *, color_tolerance: float = 40.0) -> dict[str, Any]:
    """`shots` in timeline order: {shot_key, binding, vector, rgb_mean}. Adjacent shots of the same
    binding are compared (other pairs are cuts to another place and are not judged). A pair's score
    averages the embedding cosine and the colour agreement (1 − mean RGB distance / tolerance)."""
    pairs: list[dict[str, Any]] = []
    for a, b in itertools.pairwise(shots):
        if not a.get("binding") or a.get("binding") != b.get("binding"):
            continue
        parts: list[float] = []
        cosine = _cos(list(a.get("vector") or []), list(b.get("vector") or []))
        if cosine is not None:
            parts.append(max(0.0, cosine))
        ca, cb = a.get("rgb_mean"), b.get("rgb_mean")
        color = None
        if ca and cb:
            distance = math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(ca, cb, strict=False)))
            color = max(0.0, 1.0 - distance / color_tolerance)
            parts.append(color)
        pairs.append(
            {
                "from": a.get("shot_key"),
                "to": b.get("shot_key"),
                "embedding_cosine": None if cosine is None else round(cosine, 4),
                "color_agreement": None if color is None else round(color, 4),
                "score": round(statistics.fmean(parts), 4) if parts else None,
            }
        )
    scored = [p["score"] for p in pairs if p["score"] is not None]
    return {"pairs": pairs, "score": round(min(scored), 4) if scored else None, "method": "embed_and_color_pairwise"}
