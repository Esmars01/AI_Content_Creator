"""Role-based access control by org role (§33), plus the platform-admin flag for global resources.

Roles: `owner`, `admin`, `editor`, `viewer`, `developer` (§29 `memberships.role`). A permission is
granted to a fixed set of roles; API keys additionally need the matching scope (`read` for
safe methods, `write` for everything else).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["API_KEY_SCOPES", "ROLE_RANK", "Permission", "can_assign_role", "role_has"]

ROLE_RANK = {"viewer": 0, "developer": 1, "editor": 1, "admin": 2, "owner": 3}
API_KEY_SCOPES = ("read", "write")


class Permission(StrEnum):
    READ = "read"  # every org resource
    WRITE_CONTENT = "write_content"  # projects, creators, worlds, wardrobes, memory, assets
    APPROVE_IDENTITY = "approve_identity"  # :approve on versioned identity records
    MANAGE_API_KEYS = "manage_api_keys"
    MANAGE_MEMBERS = "manage_members"  # roles, removal, invitations
    MANAGE_ORG = "manage_org"  # operator profile, org feature flags


_GRANTS: dict[Permission, frozenset[str]] = {
    Permission.READ: frozenset(ROLE_RANK),
    Permission.WRITE_CONTENT: frozenset({"editor", "admin", "owner"}),
    Permission.APPROVE_IDENTITY: frozenset({"editor", "admin", "owner"}),
    Permission.MANAGE_API_KEYS: frozenset({"developer", "admin", "owner"}),
    Permission.MANAGE_MEMBERS: frozenset({"admin", "owner"}),
    Permission.MANAGE_ORG: frozenset({"admin", "owner"}),
}


def role_has(role: str, permission: Permission) -> bool:
    return role in _GRANTS[permission]


def can_assign_role(actor_role: str, current_role: str | None, new_role: str | None) -> bool:
    """Admins manage everyone below owner; only owners grant, change or remove the owner role."""
    if actor_role == "owner":
        return True
    if actor_role != "admin":
        return False
    return "owner" not in (current_role, new_role)
