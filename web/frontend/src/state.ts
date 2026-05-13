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

export type PanRequest = {
  bounds: Bounds;
  nonce: number;
};

export type SelectedKmlTrack = {
  kmlJobId: string;
  category: "descent" | "approach" | "unclassified";
  trackIndex: number;
  name: string;
};

type AppState = {
  bounds?: Bounds;
  activeDemJob?: Job;
  activeCompareJob?: Job;
  overlay?: Overlay;
  footprints: Record<string, Footprint>;
  visibleKmls: Record<string, boolean>;
  kmlLabels: Record<string, boolean>;
  selectedKmlTrack?: SelectedKmlTrack;
  panRequest?: PanRequest;
  setBounds: (bounds?: Bounds) => void;
  setActiveDemJob: (job?: Job) => void;
  setActiveCompareJob: (job?: Job) => void;
  setOverlay: (overlay?: Overlay) => void;
  toggleFootprint: (footprint: Footprint) => void;
  clearFootprints: () => void;
  toggleKml: (jobId: string) => void;
  toggleKmlLabels: (jobId: string) => void;
  setSelectedKmlTrack: (track?: SelectedKmlTrack) => void;
  requestPan: (bounds: Bounds) => void;
};

export const useAppStore = create<AppState>((set) => ({
  footprints: {},
  visibleKmls: {},
  kmlLabels: {},
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
  clearFootprints: () => set({ footprints: {} }),
  toggleKml: (jobId) =>
    set((state) => ({
      visibleKmls: {
        ...state.visibleKmls,
        [jobId]: !state.visibleKmls[jobId]
      }
    })),
  toggleKmlLabels: (jobId) =>
    set((state) => ({
      kmlLabels: {
        ...state.kmlLabels,
        [jobId]: !state.kmlLabels[jobId]
      }
    })),
  setSelectedKmlTrack: (selectedKmlTrack) => set({ selectedKmlTrack }),
  requestPan: (bounds) => set({ panRequest: { bounds, nonce: Date.now() } })
}));
