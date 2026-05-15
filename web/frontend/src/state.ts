import { create } from "zustand";
import type { Bounds, Job, User } from "./api";

export type Overlay = {
  id: string;
  name: string;
  url: string;
  bounds: Bounds;
  tileArchiveUrl?: string;
  tileMinZoom?: number;
  tileMaxZoom?: number;
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
  activeHillshade?: Overlay;
  overlay?: Overlay;
  footprints: Record<string, Footprint>;
  visibleKmls: Record<string, boolean>;
  kmlLabels: Record<string, boolean>;
  selectedKmlTrack?: SelectedKmlTrack;
  panRequest?: PanRequest;
  activeProjectId?: string;
  authenticatedUser?: User;
  osmOpacity: number;
  esriOpacity: number;
  hillshadeOpacity: number;
  overlayOpacity: number;
  setBounds: (bounds?: Bounds) => void;
  setActiveDemJob: (job?: Job) => void;
  setActiveHillshade: (overlay?: Overlay) => void;
  setOverlay: (overlay?: Overlay) => void;
  toggleFootprint: (footprint: Footprint) => void;
  clearFootprints: () => void;
  toggleKml: (jobId: string) => void;
  toggleKmlLabels: (jobId: string) => void;
  setSelectedKmlTrack: (track?: SelectedKmlTrack) => void;
  requestPan: (bounds: Bounds) => void;
  setActiveProjectId: (id?: string) => void;
  setAuthenticatedUser: (user?: User) => void;
  setOsmOpacity: (value: number) => void;
  setEsriOpacity: (value: number) => void;
  setHillshadeOpacity: (value: number) => void;
  setOverlayOpacity: (value: number) => void;
};

const storedProjectId =
  typeof localStorage !== "undefined"
    ? localStorage.getItem("activeProjectId") || undefined
    : undefined;

export const useAppStore = create<AppState>((set) => ({
  footprints: {},
  visibleKmls: {},
  kmlLabels: {},
  activeProjectId: storedProjectId,
  osmOpacity: 1,
  esriOpacity: 0.35,
  hillshadeOpacity: 0.72,
  overlayOpacity: 0.72,
  setOsmOpacity: (osmOpacity) => set({ osmOpacity }),
  setEsriOpacity: (esriOpacity) => set({ esriOpacity }),
  setHillshadeOpacity: (hillshadeOpacity) => set({ hillshadeOpacity }),
  setOverlayOpacity: (overlayOpacity) => set({ overlayOpacity }),
  setBounds: (bounds) => set({ bounds }),
  setActiveDemJob: (activeDemJob) => set({ activeDemJob }),
  setActiveHillshade: (activeHillshade) => set({ activeHillshade }),
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
        [jobId]: !state.visibleKmls[jobId],
      },
    })),
  toggleKmlLabels: (jobId) =>
    set((state) => ({
      kmlLabels: {
        ...state.kmlLabels,
        [jobId]: !state.kmlLabels[jobId],
      },
    })),
  setSelectedKmlTrack: (selectedKmlTrack) => set({ selectedKmlTrack }),
  requestPan: (bounds) => set({ panRequest: { bounds, nonce: Date.now() } }),
  setActiveProjectId: (id) => {
    if (id) {
      localStorage.setItem("activeProjectId", id);
    } else {
      localStorage.removeItem("activeProjectId");
    }
    set({ activeProjectId: id });
  },
  setAuthenticatedUser: (user) => set({ authenticatedUser: user }),
}));
