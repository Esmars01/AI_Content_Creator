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
