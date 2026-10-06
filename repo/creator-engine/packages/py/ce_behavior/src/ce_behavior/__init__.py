"""Acting model, CBS resolver, behavior compiler, observation judgement and coverage (§15–§16).

Status: implemented and tested in Phase 3 —
- `scene`: word geometry of a scene and acting states with `carry` resolved; `acting`: the
  emotional trajectory and seconds-to-states mapping with drift;
- `inputs`: cast, world and editorial context of a version; `plan`: whole-version resolution,
  compilation against a route set and predicted coverage (no persistence);
- `resolve`: the CBS resolver (§15.6) with the §10.6 resolution order;
- `compiler`: realization methods per requested control against a target route (§15.7), plan-time
  proposals and build-time compilation; `directives`: the wire form for translators;
- `observe`: measurement (ObservedBehavior and behavior signatures); `judge`: proxy verdicts;
- `coverage`: predicted, compiled and observed reports with the 12 outcomes (§16.4);
- `qa`: Performance QA decisions and retry targets (§16.5; the ladder executes in Phase 11);
- `approximations`: stale compiler approximations after a re-route (§15.7, I1);
- `profiles`: measured behavior profiles from observations (§16.6); `lint`: no engine fields (I1);
- Phase 6: `nodes` (compile and keyframe targets shared by executors and dirty analysis) and
  `evaluate` (`BehaviorEvaluator`: old vs new outputs of behavior nodes for an edit, ADR 0041).
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 3
