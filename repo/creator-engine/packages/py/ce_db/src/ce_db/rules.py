"""Database-enforced rules (§29 conventions, I3, §12.2, §12.3), installed by migration 0001.

The rules are versioned: migration 0001 installs `V1`. A later change adds `V2` and a new
migration, so old migrations keep producing the schema they produced.

Every guard can be bypassed only inside a transaction that sets `ce.maintenance = 'on'`
(retention and deletion workflows), never by the application's normal paths.

SQLSTATEs raised (mapped to domain errors by ce_db.errors):
- `CE001` immutable record (I3, frozen manifests, video versions are never deleted);
- `CE002` append-only table;
- `CE003` cache entry pointing at a QC-rejected artifact.
"""

from __future__ import annotations

from alembic import op

__all__ = [
    "APPEND_ONLY_TABLES",
    "IMMUTABLE_TABLES",
    "VERSIONED_IDENTITY_TABLES",
    "install_database_rules",
    "remove_database_rules",
    "v1_install_sql",
    "v1_remove_sql",
]

APPEND_ONLY_TABLES = (
    "audit_logs",
    "cost_ledger",
    "creator_usage_events",
    "behavior_observations",
    "build_manifest_entries",
)
IMMUTABLE_TABLES = ("memory_snapshots",)
VERSIONED_IDENTITY_TABLES = (
    "creator_versions",
    "appearance_versions",
    "voice_versions",
    "wardrobe_versions",
    "world_versions",
    "product_versions",
)

_MAINTENANCE = "current_setting('ce.maintenance', true) = 'on'"

_FUNCTIONS = [
    f"""
CREATE OR REPLACE FUNCTION ce_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF {_MAINTENANCE} THEN RETURN COALESCE(NEW, OLD); END IF;
  RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP USING ERRCODE = 'CE002';
END $$""",
    f"""
CREATE OR REPLACE FUNCTION ce_guard_approved_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF {_MAINTENANCE} THEN RETURN COALESCE(NEW, OLD); END IF;
  IF OLD.status IN ('approved', 'archived') THEN
    IF TG_OP = 'DELETE' THEN
      RAISE EXCEPTION '%: approved versions cannot be deleted (I3)', TG_TABLE_NAME USING ERRCODE = 'CE001';
    END IF;
    IF OLD.status = 'approved' AND NEW.status = 'archived'
       AND (to_jsonb(NEW) - 'status' - 'updated_at') = (to_jsonb(OLD) - 'status' - 'updated_at') THEN
      RETURN NEW;
    END IF;
    RAISE EXCEPTION '%: approved versions are immutable (I3); create a new draft version', TG_TABLE_NAME
      USING ERRCODE = 'CE001';
  END IF;
  RETURN COALESCE(NEW, OLD);
END $$""",
    f"""
CREATE OR REPLACE FUNCTION ce_guard_video_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF {_MAINTENANCE} THEN RETURN COALESCE(NEW, OLD); END IF;
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'video versions are never deleted (§12.8)' USING ERRCODE = 'CE001';
  END IF;
  IF NEW.spec IS DISTINCT FROM OLD.spec
     OR NEW.spec_hash IS DISTINCT FROM OLD.spec_hash
     OR NEW.spec_content_digest IS DISTINCT FROM OLD.spec_content_digest
     OR NEW.parent_version_id IS DISTINCT FROM OLD.parent_version_id
     OR NEW.origin IS DISTINCT FROM OLD.origin
     OR NEW.planned_routes IS DISTINCT FROM OLD.planned_routes
     OR NEW.video_id IS DISTINCT FROM OLD.video_id
     OR NEW.number IS DISTINCT FROM OLD.number
     OR NEW.org_id IS DISTINCT FROM OLD.org_id THEN
    RAISE EXCEPTION 'identity columns of video versions are immutable (I3, §12.8)' USING ERRCODE = 'CE001';
  END IF;
  RETURN NEW;
END $$""",
    f"""
CREATE OR REPLACE FUNCTION ce_manifest_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF {_MAINTENANCE} THEN RETURN NEW; END IF;
  IF EXISTS (SELECT 1 FROM video_versions v WHERE v.id = NEW.version_id AND v.frozen_at IS NOT NULL) THEN
    RAISE EXCEPTION 'the BuildManifest of a frozen version cannot change (§12.3)' USING ERRCODE = 'CE001';
  END IF;
  RETURN NEW;
END $$""",
    """
CREATE OR REPLACE FUNCTION ce_cache_entry_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM artifacts a WHERE a.id = NEW.artifact_id AND a.qc_state = 'qc_rejected') THEN
    RAISE EXCEPTION 'cache entries never point at QC-rejected artifacts (§12.2)' USING ERRCODE = 'CE003';
  END IF;
  RETURN NEW;
END $$""",
    """
CREATE OR REPLACE FUNCTION ce_evict_rejected_artifact() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.qc_state = 'qc_rejected' AND OLD.qc_state IS DISTINCT FROM 'qc_rejected' THEN
    DELETE FROM cache_entries WHERE org_id = NEW.org_id AND artifact_id = NEW.id;
  END IF;
  RETURN NEW;
END $$""",
    """
CREATE OR REPLACE FUNCTION ce_touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$""",
]


def v1_install_sql(tables_with_updated_at: list[str]) -> list[str]:
    statements = [f.strip() for f in _FUNCTIONS]
    for table in APPEND_ONLY_TABLES + IMMUTABLE_TABLES:
        statements.append(
            f"CREATE TRIGGER ce_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ce_append_only()"
        )
    for table in VERSIONED_IDENTITY_TABLES:
        statements.append(
            f"CREATE TRIGGER ce_{table}_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ce_guard_approved_version()"
        )
    statements += [
        "CREATE TRIGGER ce_video_versions_guard BEFORE UPDATE OR DELETE ON video_versions "
        "FOR EACH ROW EXECUTE FUNCTION ce_guard_video_version()",
        "CREATE TRIGGER ce_build_manifest_entries_frozen BEFORE INSERT ON build_manifest_entries "
        "FOR EACH ROW EXECUTE FUNCTION ce_manifest_insert_guard()",
        "CREATE TRIGGER ce_cache_entries_guard BEFORE INSERT OR UPDATE ON cache_entries "
        "FOR EACH ROW EXECUTE FUNCTION ce_cache_entry_guard()",
        "CREATE TRIGGER ce_artifacts_evict_rejected AFTER UPDATE OF qc_state ON artifacts "
        "FOR EACH ROW EXECUTE FUNCTION ce_evict_rejected_artifact()",
    ]
    for table in tables_with_updated_at:
        if table in APPEND_ONLY_TABLES + IMMUTABLE_TABLES:
            continue
        statements.append(
            f"CREATE TRIGGER ce_{table}_touch BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ce_touch_updated_at()"
        )
    return statements


def v1_remove_sql() -> list[str]:
    return [
        f"DROP FUNCTION IF EXISTS {name}() CASCADE"
        for name in (
            "ce_append_only",
            "ce_guard_approved_version",
            "ce_guard_video_version",
            "ce_manifest_insert_guard",
            "ce_cache_entry_guard",
            "ce_evict_rejected_artifact",
            "ce_touch_updated_at",
        )
    ]


# Tables that have an updated_at column at revision 0001 (all 75 tables of §29).
_V1_TABLES = [
    "organizations",
    "users",
    "memberships",
    "invitations",
    "sessions",
    "api_keys",
    "idempotency_keys",
    "operator_profiles",
    "projects",
    "videos",
    "video_versions",
    "build_manifest_entries",
    "scenes",
    "scene_cast",
    "shots",
    "takes",
    "edit_proposals",
    "director_runs",
    "critiques",
    "packaging",
    "renders",
    "exports",
    "captions",
    "creators",
    "creator_versions",
    "appearances",
    "appearance_versions",
    "voices",
    "voice_versions",
    "voice_candidates",
    "voice_conditioning",
    "wardrobes",
    "wardrobe_versions",
    "creator_tests",
    "creator_baselines",
    "consistency_reports",
    "creator_memory_items",
    "memory_snapshots",
    "creator_usage_events",
    "worlds",
    "world_versions",
    "products",
    "product_versions",
    "behavior_observations",
    "model_behavior_profiles",
    "qc_reports",
    "human_ratings",
    "assets",
    "artifacts",
    "cache_entries",
    "artifact_refs",
    "generation_jobs",
    "execution_nodes",
    "job_attempts",
    "gpu_tasks",
    "notifications",
    "research_sources",
    "research_facts",
    "claims",
    "spec_templates",
    "brand_kits",
    "consents",
    "protected_persons",
    "retention_policies",
    "plugins",
    "models",
    "model_benchmarks",
    "benchmark_pairs",
    "gpu_providers",
    "gpu_workers",
    "worker_enrollment_tokens",
    "cost_ledger",
    "feature_flags",
    "org_feature_flags",
    "audit_logs",
]


def install_database_rules() -> None:
    """Called by migration 0001."""
    for statement in v1_install_sql(list(_V1_TABLES)):
        op.execute(statement)


def remove_database_rules() -> None:
    for statement in v1_remove_sql():
        op.execute(statement)
