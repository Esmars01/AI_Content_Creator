"""Creator Memory (§18).

Status: implemented and tested in Phase 4 —
- `retrieval`: `MemoryRetriever` over structured filters + keyword ranking (embedding ranking is
  Phase 12, behind the same interface), recency decay, confidence weighting, pinned first,
  per-category budgets, conflict resolution (pinned > authored > confidence > recency);
- `store`: loading items and writing immutable MemorySnapshots (I7) and usage events;
- `usage`: the usage log entry of a ready version (hooks, phrase fingerprints, arc, visual and
  behavior signatures, worlds, wardrobes);
- `repetition`: the repetition guard over the creator's recent distinct videos (keyword and
  n-gram similarity until Phase 12);
- `contradictions`: script vs canon and persona memory, memory vs memory, DNA vs memory.

Phase 12 (ADR 0057) adds the write-path decisions of `MemoryUpdateWorkflow` (`loop`: persona
proposals after approval, activation after export, observation habits merged per video and
promoted only past the thresholds and never from mock evidence, preferences from repeated edits),
embeddings in retrieval and the repetition guard (cosine of vectors of the same model; keyword
paths when none), and conflict linking shared with the API (`store.link_conflicts`).
"""

from ce_memory.contradictions import Contradiction, PersonaAssertion, dna_conflicts, script_contradictions
from ce_memory.records import MemoryRecord
from ce_memory.repetition import RepetitionFinding, RepetitionGuard, UsageEntry
from ce_memory.retrieval import MemoryRetriever, RetrievalContext, SnapshotDraft
from ce_memory.text import keywords, ngram_fingerprints, similarity
from ce_memory.usage import usage_payload

__all__ = [
    "Contradiction",
    "MemoryRecord",
    "MemoryRetriever",
    "PersonaAssertion",
    "RepetitionFinding",
    "RepetitionGuard",
    "RetrievalContext",
    "SnapshotDraft",
    "UsageEntry",
    "dna_conflicts",
    "keywords",
    "ngram_fingerprints",
    "script_contradictions",
    "similarity",
    "usage_payload",
]

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 4
