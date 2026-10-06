## What and why

<!-- One logical change (rule 17). Link the phase / TODO item. -->

## Tested how

<!-- Commands run and results. Say what is untested and why (rule 5). -->

## Rule 20: no orphan concepts

If this change adds an entity, schema, capability, memory kind, vocabulary item, QC metric or EditOperation,
tick every place it applies, or link the tracked TODO:

- [ ] Pydantic models (`ce_core` / `ce_contracts`)
- [ ] VideoSpec or CBS schema (+ JSON Schema snapshot)
- [ ] DB migration (`ce_db`)
- [ ] API endpoint and OpenAPI examples
- [ ] UI
- [ ] Plugin manifest or adapter interface
- [ ] Build-graph dependencies (node kinds, cache keys, dirty analysis)
- [ ] Router
- [ ] Tests (unit / invariant / behavior suite)
- [ ] Docs (`docs/*.md`, ADR if a real alternative was chosen)
- [ ] Phase or roadmap entry (`TODO.md`, `ROADMAP.md`)

## Invariants

- [ ] No invariant in §4 is weakened (or an ADR and an updated invariant are included — rule 19)
