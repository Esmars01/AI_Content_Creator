-- Runs once when the Postgres volume is first initialized (dev/test only).
-- Phase 1 migrations also create the extension, so this is a convenience, not the source of truth.
CREATE EXTENSION IF NOT EXISTS vector;
