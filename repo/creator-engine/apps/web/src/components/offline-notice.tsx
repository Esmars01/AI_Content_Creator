"use client";

/**
 * Says so while the browser is offline. Requests started meanwhile wait (TanStack Query pauses
 * them) and are sent once the connection is back; without this note a button just read
 * "Sending…" with no reason (audit BREAK-OFFLINE).
 */
import { useSyncExternalStore } from "react";
import { Alert } from "@/components/ui/misc";

function subscribe(onChange: () => void): () => void {
  window.addEventListener("online", onChange);
  window.addEventListener("offline", onChange);
  return () => {
    window.removeEventListener("online", onChange);
    window.removeEventListener("offline", onChange);
  };
}

export function useOnline(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => navigator.onLine,
    () => true,
  );
}

export function OfflineNotice() {
  const online = useOnline();
  if (online) return null;
  return (
    <Alert tone="warning" data-testid="offline-notice" className="mb-4">
      You are offline. What you send now waits and goes out when the connection is back.
    </Alert>
  );
}
