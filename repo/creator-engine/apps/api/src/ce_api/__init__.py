"""FastAPI service: public REST + SSE, auth, admin CLI (`ce`).

Status: Phase 1 API skeleton implemented and tested — auth (password provider, sessions, CSRF,
API keys), members and invitations, projects, videos/versions (read), creators, appearances,
wardrobes, voices (read), worlds, memory, multipart asset upload with validation, problem+json,
OpenAPI, SSE. Generation, planning and editing endpoints arrive with their phases (ROADMAP.md).
"""

__version__ = "0.2.0"
IMPLEMENTED_IN_PHASE = 1
