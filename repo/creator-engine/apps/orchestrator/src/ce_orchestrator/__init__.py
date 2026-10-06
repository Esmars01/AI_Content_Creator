"""Temporal worker: workflows and CPU activities; `cpu_inproc` plugins in-process (§9).

Status: implemented and tested in Phase 2: `GenerateVersionWorkflow` (with one
`SceneBuildWorkflow` per scene), `RenderWorkflow`, `AssetValidationWorkflow` and
`DeletionWorkflow`. Director stages (Phase 4), previz (Phase 5) and editing (Phase 6) follow.
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
