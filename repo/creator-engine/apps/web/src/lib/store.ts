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
  /** The proposal card on display (an NL edit, an Advanced editor change, a lock or take change). */
  activeProposal: string | null;
  selectScene: (key: string | null) => void;
  setDeveloperOpen: (open: boolean) => void;
  showProposal: (id: string | null) => void;
}

export const useStudio = create<StudioState>()((set) => ({
  selectedScene: null,
  developerOpen: false,
  activeProposal: null,
  selectScene: (key) => set({ selectedScene: key }),
  setDeveloperOpen: (open) => set({ developerOpen: open }),
  showProposal: (id) => set({ activeProposal: id }),
}));
