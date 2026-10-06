"""SQLAlchemy models, Alembic migrations, org-scoped repositories.

Status: implemented and tested in Phase 1 (all tables, migration 0001 with database rules,
org-scoped repositories, projections). Phase 2 adds the scheduler's GPU queue (`ce_db.queue`) and
the build execution records (`ce_db.execution`); Phase 6 the derived-version factory
(`ce_db.versions`).
"""

__version__ = "0.2.0"
IMPLEMENTED_IN_PHASE = 1
