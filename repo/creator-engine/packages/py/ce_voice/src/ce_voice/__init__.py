"""Voice and speech text processing (§21).

Status: Phase 4 implements and tests the acting-tag parser (`tags`) and exact-script extraction
with byte-equality verification (`exact`). Phase 7 adds the language normalizers (`normalize`,
`numbers`: en, de, es, fr, it, ru, tr, az, ar, with source-token mapping) and normalized WER/CER
(`metrics`) for the exact-script verification loop over real CPU TTS and ASR.
"""

from ce_voice.exact import ExactScriptError, ExactSegment, extract_segments, sentence_spans
from ce_voice.metrics import ErrorRates, cer, script_error_rates, wer
from ce_voice.normalize import NORMALIZER_VERSION, Normalized, SpokenToken, comparison_words, fold, normalize
from ce_voice.numbers import cardinal
from ce_voice.tags import CANONICAL_TAGS, TaggedText, TagSpec, parse_tags, reassemble

__all__ = [
    "CANONICAL_TAGS",
    "NORMALIZER_VERSION",
    "ErrorRates",
    "ExactScriptError",
    "ExactSegment",
    "Normalized",
    "SpokenToken",
    "TagSpec",
    "TaggedText",
    "cardinal",
    "cer",
    "comparison_words",
    "extract_segments",
    "fold",
    "normalize",
    "parse_tags",
    "reassemble",
    "script_error_rates",
    "sentence_spans",
    "wer",
]

__version__ = "0.2.0"
IMPLEMENTED_IN_PHASE = 7
