"""Model results → node output documents, with the per-kind additions downstream nodes need."""

from __future__ import annotations

import re
from typing import Any

from ce_contracts.capabilities import capability as capability_spec
from ce_contracts.common import ArtifactRef
from ce_core.text import tokenize
from ce_voice.metrics import script_error_rates
from pydantic import BaseModel

from ce_exec.noderun import NodeRun
from ce_exec.outputs import NodeOutput

__all__ = ["output_from_result", "word_error_rate"]

_PUNCT = re.compile(r"[^\w\s']", re.UNICODE)


def _norm(text: str) -> list[str]:
    return [w for w in _PUNCT.sub(" ", text.lower()).split() if w]


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Levenshtein distance over normalized words ÷ reference length (exact-script verification)."""
    ref, hyp = _norm(reference), _norm(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, start=1):
        current = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, start=1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (r != h))
        previous = current
    return round(previous[-1] / len(ref), 4)


def _split(result: BaseModel) -> tuple[dict[str, ArtifactRef], dict[str, Any]]:
    refs: dict[str, ArtifactRef] = {}
    data: dict[str, Any] = {}
    for name in type(result).model_fields:
        value = getattr(result, name)
        if isinstance(value, ArtifactRef):
            refs[name] = value
        elif isinstance(value, list) and value and all(isinstance(v, ArtifactRef) for v in value):
            for i, v in enumerate(value):
                refs[f"{name}.{i}"] = v
        elif value is not None:
            data[name] = (
                value.model_dump(mode="json")
                if isinstance(value, BaseModel)
                else (
                    [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value]
                    if isinstance(value, list)
                    else value
                )
            )
    return refs, data


def output_from_result(
    run: NodeRun, capability: str, result: dict[str, Any] | BaseModel, extra: dict[str, Any]
) -> NodeOutput:
    model = result if isinstance(result, BaseModel) else capability_spec(capability).result.model_validate(result)
    refs, data = _split(model)
    data.update(extra)
    kind = run.node.kind
    if kind == "align.segment":
        tts = run.dep("tts.segment:")
        words = sorted(data.pop("words", []), key=lambda w: w["index"])
        expected = len(tokenize(run.spec.script.segment(run.node.segment_key or "").text))
        if len(words) != expected:
            raise ValueError(f"{run.node.key}: the aligner returned {len(words)} words, the script has {expected}")
        data = {
            "words": [[round(float(w["start_s"]), 4), round(float(w["end_s"]), 4)] for w in words],
            "confidence": [float(w.get("confidence", 1.0)) for w in words],
            "precision": data.get("precision", "fine"),
            "duration_s": float(tts.data["duration_s"]),
        }
        refs["audio"] = tts.ref("audio")
    elif kind == "asr.verify":
        segment = run.spec.script.segment(run.node.segment_key or "")
        language = segment.language or run.spec.meta.language
        repeats = any(
            str(a.type) == "inserted_disfluency" and str(a.tag) in ("repetition", "false_start")
            for a in segment.annotations
        )
        rates = script_error_rates(segment.text, str(data.get("text", "")), language, allow_repetitions=repeats)
        max_wer = float(run.node.params.get("max_wer", 0.08))
        max_cer = float(run.node.params.get("max_cer", 0.03))
        data.update(
            {
                "wer": rates.wer,
                "cer": rates.cer,
                "max_wer": max_wer,
                "max_cer": max_cer,
                "reference_words": list(rates.reference),
                "heard_words": list(rates.hypothesis),
                # a compound written as one word or two, or one misheard letter, costs WER but not CER
                "passed": rates.wer <= max_wer or rates.cer <= max_cer,
            }
        )
    return NodeOutput(node_kind=kind, data=data, refs=refs)
