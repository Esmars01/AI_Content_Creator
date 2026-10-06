"""The AI Director (§13–§15): typed stages from free text to a validated VideoSpec, the intent
policy engine, mode templates and strategy packs (config), and the labeled template Director.

Status: implemented and tested in Phase 4 —
- `director`: stages 1–11 (`Director.plan`), the repair loop and closed-vocabulary mapping (I13),
  data wrapping (I10), plan-time routing and compiler proposals, the plan report;
- `intent_policy`: the deterministic rule engine with `derived_from: intent` traceability;
- `template`: the template Director for free-form dev input (`planner: template`);
- `store`: loading the planning context from the database and persisting a plan.
Phase 6 adds `edit` (reference resolution, the `edit` stage with slots, the labeled template
planner) and `proposal` (`compute_proposal`: patch, validation, impact, coverage delta and
alternatives, ADR 0040). Phase 12 adds persistent research facts, embeddings and claim checks
in code in stages 3–6 (ADR 0058), the project brand default (ADR 0059) and `packaging` — stage
12: limits, validation and the labelled template packaging (the job lives in
`ce_exec.packaging_job`).
"""

from ce_director.context import BrandDefault, ContextPack, CreatorOption, DirectorContext, WorldOption
from ce_director.director import Director, DirectorDeps, PlanningError, PlanOutcome
from ce_director.intent_policy import IntentPolicyEngine, PolicyDecision, PolicySet, Proposal
from ce_director.models import PlanRequest

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 4

__all__ = [
    "BrandDefault",
    "ContextPack",
    "CreatorOption",
    "Director",
    "DirectorContext",
    "DirectorDeps",
    "IntentPolicyEngine",
    "PlanOutcome",
    "PlanRequest",
    "PlanningError",
    "PolicyDecision",
    "PolicySet",
    "Proposal",
    "WorldOption",
]
