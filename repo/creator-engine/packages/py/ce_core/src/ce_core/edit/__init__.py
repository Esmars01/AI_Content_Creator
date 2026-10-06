"""Incremental editing (§28, ADR 0025): the `EditOperation` union, SpecPatch, lock checks (I8),
anchor rebase, the structured spec diff and the operation → patch translation."""

from ce_core.edit.diff import DiffEntry, area_of, spec_diff
from ce_core.edit.locks import (
    LockViolation,
    RegenerateRefusal,
    describe_scope,
    expand_lock,
    lock_applies,
    lock_violations,
    regenerate_refusals,
)
from ce_core.edit.ops import EDIT_OPERATION_TYPES, EditOperation, EditScope, operation_list, parse_operations
from ce_core.edit.patch import PatchError, PatchOp, SpecPatch, apply_patch
from ce_core.edit.rebase import RebaseReport, rebase_segment, word_map

__all__ = [
    "EDIT_OPERATION_TYPES",
    "DiffEntry",
    "EditOperation",
    "EditScope",
    "LockViolation",
    "PatchError",
    "PatchOp",
    "RebaseReport",
    "RegenerateRefusal",
    "SpecPatch",
    "apply_patch",
    "area_of",
    "describe_scope",
    "expand_lock",
    "lock_applies",
    "lock_violations",
    "operation_list",
    "parse_operations",
    "rebase_segment",
    "regenerate_refusals",
    "spec_diff",
    "word_map",
]
