"use client";
/** The note shown above write controls a member's role disables ("Your role (viewer) can view…"). */
import { Alert } from "@/components/ui/misc";
import { type Permission, useCan } from "@/lib/roles";

export function RoleNote({ permission = "write_content", className }: { permission?: Permission; className?: string }) {
  const can = useCan(permission);
  if (can.allowed) return null;
  return (
    <Alert tone="info" className={className} data-testid="role-note">
      {can.reason}
    </Alert>
  );
}
