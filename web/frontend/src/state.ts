import { create } from "zustand";
import type { Bounds, Job } from "./api";

export type Overlay = {
  id: string;
  name: string;
  url: string;
  bounds: Bounds;
};

export type Footprint = {
  id: string;
  name: string;
  bounds: Bounds;
};

type AppState = {
  bounds?: Bounds;
  activeDemJob?: Job;
  activeCompareJob?: Job;
  overlay?: Overlay;
  footprints: Record<string, Footprint>;
  setBounds: (bounds?: Bounds) => void;
  setActiveDemJob: (job?: Job) => void;
  setActiveCompareJob: (job?: Job) => void;
  setOverlay: (overlay?: Overlay) => void;
  toggleFootprint: (footprint: Footprint) => void;
  clearFootprints: () => void;
};

export const useAppStore = create<AppState>((set) => ({
  footprints: {},
  setBounds: (bounds) => set({ bounds }),
  setActiveDemJob: (activeDemJob) => set({ activeDemJob }),
  setActiveCompareJob: (activeCompareJob) => set({ activeCompareJob }),
  setOverlay: (overlay) => set({ overlay }),
  toggleFootprint: (footprint) =>
    set((state) => {
      const next = { ...state.footprints };
      if (next[footprint.id]) {
        delete next[footprint.id];
      } else {
        next[footprint.id] = footprint;
      }
      return { footprints: next };
    }),
  clearFootprints: () => set({ footprints: {} })
}));
