"use client";
/**
 * What the signed-in member's org role may do, mirroring the API's RBAC table
 * (`apps/api/src/ce_api/security/rbac.py`). Display only: the API still decides; this keeps write
 * controls disabled, with the reason, for roles the API would refuse with a 403.
 */
import { useMe } from "./queries";

export type Permission =
  "read" | "write_content" | "approve_identity" | "manage_api_keys" | "manage_members" | "manage_org";

const GRANTS: Record<Permission, readonly string[]> = {
  read: ["viewer", "developer", "editor", "admin", "owner"],
  write_content: ["editor", "admin", "owner"],
  approve_identity: ["editor", "admin", "owner"],
  manage_api_keys: ["developer", "admin", "owner"],
  manage_members: ["admin", "owner"],
  manage_org: ["admin", "owner"],
};

export function roleHas(role: string, permission: Permission): boolean {
  return GRANTS[permission].includes(role);
}

/** Why `role` may not do it, in words (shown as a note and as the disabled control's title). */
export function deniedReason(role: string, permission: Permission): string {
  switch (permission) {
    case "write_content":
      return `Your role (${role}) can view but not change content.`;
    case "approve_identity":
      return `Your role (${role}) can view but not approve.`;
    case "manage_api_keys":
      return `Your role (${role}) cannot manage API keys.`;
    default:
      return `Your role (${role}) cannot change organization settings.`;
  }
}

export interface Can {
  allowed: boolean;
  role: string | null;
  /** set when not allowed */
  reason: string | undefined;
}

/**
 * Whether the member may do `permission`. While `/v1/me` is unknown the controls stay enabled
 * (the API answers anyway); a known role without the permission disables them.
 */
export function useCan(permission: Permission): Can {
  const me = useMe();
  const role = me.data?.role ?? null;
  if (!role || roleHas(role, permission)) return { allowed: true, role, reason: undefined };
  return { allowed: false, role, reason: deniedReason(role, permission) };
}
