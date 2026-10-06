"""License policy, consent, blocklists, testimonial guard, provenance rules (§32).

Status:
- Phase 2: the license policy engine (`ce_policy.license.evaluate` over a model's license closure
  and the operator profile) and the provenance-mode rule (`ce_policy.provenance`);
- Phase 4: blocklist checks on the Director's output (`ce_policy.blocklist`) and testimonial-guard
  detection with rules plus an LLM classification (`ce_policy.testimonial`).
Likeness checks against `protected_persons`, the guard's review UI and quote-consent verification are not
implemented (Phase 13 was skipped; docs/POLICY_AND_COMPLIANCE.md lists the gaps).
"""

from ce_policy.blocklist import BlocklistChecker, BlocklistHit
from ce_policy.license import Decision, OperatorProfile, evaluate
from ce_policy.provenance import (
    AI_LABEL,
    MOCK_LABEL,
    ProvenanceRefused,
    burned_labels,
    require_provenance_mode,
    resolve_visible_label,
)
from ce_policy.testimonial import GuardResult, TestimonialGuard, has_disclosure

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 4

__all__ = [
    "AI_LABEL",
    "MOCK_LABEL",
    "BlocklistChecker",
    "BlocklistHit",
    "Decision",
    "GuardResult",
    "OperatorProfile",
    "ProvenanceRefused",
    "TestimonialGuard",
    "burned_labels",
    "evaluate",
    "has_disclosure",
    "require_provenance_mode",
    "resolve_visible_label",
]
