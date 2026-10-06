"""The Creator Test scorecard and baselines (§17.4, §20). Pure functions over what the test video's
build and the scoring calls produced; nothing here runs a model.

Every metric says how it was measured and whether a mock produced it (`mock: true`): a mock score is
a pipeline check, never a statement about the creator. Metrics the system cannot measure are present
with `status: not_measured` and the reason — accent (a human rating; ASR language ID cannot tell
accents apart), and anything whose analyzer did not run.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

__all__ = ["baseline_stats", "cosine", "distribution", "scorecard"]


def cosine(a: Sequence[float], b: Sequence[float]) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return None
    return round(sum(x * y for x, y in zip(a, b, strict=True)) / (na * nb), 4)


def distribution(values: Iterable[float | None]) -> dict[str, Any]:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return {"status": "not_measured", "n": 0}
    return {
        "status": "measured",
        "n": len(clean),
        "min": round(clean[0], 4),
        "p50": round(statistics.median(clean), 4),
        "mean": round(statistics.fmean(clean), 4),
        "max": round(clean[-1], 4),
    }


def _not_measured(reason: str) -> dict[str, Any]:
    return {"status": "not_measured", "reason": reason}


def scorecard(
    *,
    build_state: str,
    verify: Sequence[Mapping[str, Any]],
    tts: Sequence[Mapping[str, Any]],
    qc_shots: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
    qc_world: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    face_similarity: Sequence[float | None],
    face_mock: bool,
    voice_similarity: Sequence[float | None],
    voice_mock: bool,
    voice_basis: str,
    speech_quality: Sequence[Mapping[str, Any]],
    critique: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The §17.4 scorecard. `tts` items carry `words` and `duration_s`; `verify` items are the
    `asr.verify` outputs; `qc_shots` the `qc.shot` outputs; `observations` the `behavior.observe`
    outputs; `qc_world` the `qc.world` outputs; `coverage` the `behavior.coverage` output."""
    card: dict[str, Any] = {"build_state": build_state}
    card["identity_similarity"] = {
        **distribution(face_similarity),
        "method": "face.embed cosine vs the canonical face, per keyframe",
        "mock": face_mock,
    }
    if voice_similarity:
        card["voice_similarity"] = {**distribution(voice_similarity), "method": voice_basis, "mock": voice_mock}
    else:
        card["voice_similarity"] = _not_measured(voice_basis)
    lipsync: list[dict[str, Any]] = [
        dict(dict(s.get("metrics", {}))["qc.lipsync"]) for s in qc_shots if dict(s.get("metrics", {})).get("qc.lipsync")
    ]
    if lipsync:
        card["lipsync"] = {
            **distribution(float(m["score"]) for m in lipsync),
            "metric": lipsync[0].get("metric"),
            "advisory": True,  # §26: advisory until its license is resolved
            "mock": any(bool(dict(m.get("metrics", {})).get("mock")) for m in lipsync),
        }
    else:
        card["lipsync"] = _not_measured("no lip-sync metric ran on the test takes")
    wers = [float(v["wer"]) for v in verify if v.get("wer") is not None]
    card["wer"] = (
        {**distribution(wers), "passed": all(bool(v.get("passed")) for v in verify), "segments": len(verify)}
        if wers
        else _not_measured("no ASR verification ran")
    )
    if speech_quality:
        card["speech_quality"] = {
            **distribution(float(q["score"]) for q in speech_quality),
            "metric": speech_quality[0].get("metric"),
            "mock": any(bool(q.get("mock")) for q in speech_quality),
        }
    else:
        card["speech_quality"] = _not_measured("no speech-quality metric ran")
    words = sum(int(t.get("words", 0)) for t in tts)
    seconds = sum(float(t.get("duration_s", 0.0)) for t in tts)
    card["wpm"] = (
        {
            "status": "measured",
            "value": round(words * 60.0 / seconds, 1),
            "words": words,
            "seconds": round(seconds, 3),
            "method": "canonical words ÷ synthesized dialogue duration",
        }
        if seconds > 0
        else _not_measured("no dialogue was synthesized")
    )
    card["accent"] = _not_measured("a human rating: ASR language ID confirms the language, not the accent (§17.4)")
    head = [
        statistics.fmean(sig["head_motion_pattern"])
        for o in observations
        if (sig := dict(dict(o.get("observed", {})).get("signature", {}))).get("head_motion_pattern")
    ]
    faces = [
        float(t["face_detected_ratio"])
        for o in observations
        for t in dict(o.get("observed", {})).get("tracks", [])
        if t.get("face_detected_ratio") is not None
    ]
    card["motion"] = {
        "head_motion_energy": distribution(head),
        "face_detected_ratio": distribution(faces),
        "flags": (["frozen"] if head and max(head) < 0.2 else []) + (["frantic"] if head and min(head) > 15 else []),
    }
    identity = [float(w["environment_identity"]) for w in qc_world if w.get("environment_identity") is not None]
    card["world_fidelity"] = (
        {**distribution(identity), "method": "qc.world environment identity (reliability medium/low, §19.6)"}
        if identity
        else _not_measured("no world QC ran")
    )
    if coverage:
        entries = list(dict(coverage.get("report", {})).get("entries", []))
        outcomes: dict[str, int] = {}
        for entry in entries:
            outcomes[str(entry.get("outcome"))] = outcomes.get(str(entry.get("outcome")), 0) + 1
        card["requested_vs_observed"] = {
            "items": len(entries),
            "outcomes": outcomes,
            "confirmed": sum(n for k, n in outcomes.items() if k.endswith("_CONFIRMED") or k == "CONFIRMED"),
        }
    else:
        card["requested_vs_observed"] = _not_measured("the coverage report did not build")
    card["vlm_critique"] = dict(critique) if critique else _not_measured("no VLM critique ran")
    card["human_rating"] = _not_measured("rate the test (1–5) in the Creator Test tab")
    card["same_person_rating"] = _not_measured("rate whether the test shows the same person as the canonical face")
    return card


def baseline_stats(card: Mapping[str, Any]) -> dict[str, Any]:
    """The values a baseline keeps from one scorecard (§20): medians of the measured metrics."""
    out: dict[str, Any] = {}
    for key in ("identity_similarity", "voice_similarity", "lipsync", "wer", "speech_quality", "world_fidelity"):
        metric = dict(card.get(key) or {})
        if metric.get("status") == "measured":
            out[key] = {"p50": metric.get("p50"), "n": metric.get("n"), "mock": bool(metric.get("mock"))}
    wpm = dict(card.get("wpm") or {})
    if wpm.get("status") == "measured":
        out["wpm"] = wpm.get("value")
    motion = dict(card.get("motion") or {})
    head = dict(motion.get("head_motion_energy") or {})
    if head.get("status") == "measured":
        out["head_motion_energy"] = head.get("p50")
    return out
