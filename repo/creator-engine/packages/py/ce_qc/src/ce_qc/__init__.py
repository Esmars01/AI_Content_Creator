"""QC: checks with thresholds keyed by metric adapter, the decision ladder and its budgets, the VLM
judge, creator consistency, critique and benchmark verdicts (§26, §20, §24).

Status: implemented and tested in Phase 11 with the mock engines (ADR 0056). Pure logic only: the
build's QC gate (`ce_exec.qcgate`) and the jobs that read and write the database use it.

- `thresholds`: metric verdicts keyed by adapter id;
- `ladder`: attempt seed → fallback route → cheaper fix → needs_review, with count and USD budgets;
- `vlm_judge`: structured prompts and answer validation;
- `consistency`: per-video features, rolling baselines and bands (§20).
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 11
