"use client";
/**
 * The Studio without `?version=` shows the video's current version, and follows it when another
 * tab or person creates a newer one. Once a proposal is open on the version shown, the URL is
 * pinned to that version: otherwise the view moved on and the proposal card vanished without a
 * word (audit BREAK-TWO-TABS).
 */
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";
import { useActiveProposal } from "@/lib/store";

export function usePinShownVersion(versionId: string | null, pinned: boolean): void {
  const router = useRouter();
  const pathname = usePathname();
  const active = useActiveProposal(versionId ?? "");
  useEffect(() => {
    if (versionId && active && !pinned) router.replace(`${pathname}?version=${versionId}`, { scroll: false });
  }, [versionId, active, pinned, router, pathname]);
}
