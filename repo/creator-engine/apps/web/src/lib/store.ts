"use client";
/** Editor and view state (Zustand). The Simple/Advanced choice is remembered per user (§31). */
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

interface Prefs {
  advancedByUser: Record<string, boolean>;
  setAdvanced: (userId: string, advanced: boolean) => void;
}

export const usePrefs = create<Prefs>()(
  persist(
    (set) => ({
      advancedByUser: {},
      setAdvanced: (userId, advanced) =>
        set((state) => ({ advancedByUser: { ...state.advancedByUser, [userId]: advanced } })),
    }),
    { name: "ce-prefs", storage: createJSONStorage(() => localStorage) },
  ),
);

interface StudioState {
  selectedScene: string | null;
  developerOpen: boolean;
  /**
   * The proposal card on display per version (an NL edit, an Advanced editor change, a lock or take
   * change). Keyed by the version it was proposed on, so another video's or version's Studio never
   * shows (and never applies) it.
   */
  activeProposals: Record<string, string | null>;
  selectScene: (key: string | null) => void;
  setDeveloperOpen: (open: boolean) => void;
  showProposal: (versionId: string, id: string | null) => void;
}

export const useStudio = create<StudioState>()((set) => ({
  selectedScene: null,
  developerOpen: false,
  activeProposals: {},
  selectScene: (key) => set({ selectedScene: key }),
  setDeveloperOpen: (open) => set({ developerOpen: open }),
  showProposal: (versionId, id) => set((state) => ({ activeProposals: { ...state.activeProposals, [versionId]: id } })),
}));

/** The proposal shown on `versionId`'s edit panel, if any. */
export const useActiveProposal = (versionId: string): string | null =>
  useStudio((state) => state.activeProposals[versionId] ?? null);

interface PendingVersions {
  /** version id → the job that creates it (an applied edit, a restore, a branch, a translation…) */
  jobs: Record<string, string>;
  expectVersion: (versionId: string, jobId: string) => void;
}

/**
 * Versions this tab asked the API to create and that may not exist yet: until their job ends, the
 * Studio shows "Creating the new version…" for a 404 instead of "This version does not exist".
 * Kept for the session (a reload right after Apply still knows), at most the 20 latest.
 */
export const usePendingVersions = create<PendingVersions>()(
  persist(
    (set) => ({
      jobs: {},
      expectVersion: (versionId, jobId) =>
        set((state) => ({ jobs: Object.fromEntries([...Object.entries(state.jobs), [versionId, jobId]].slice(-20)) })),
    }),
    { name: "ce-pending-versions", storage: createJSONStorage(() => sessionStorage) },
  ),
);
