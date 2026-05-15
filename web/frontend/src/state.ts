import { create } from "zustand";
import type { Bounds, Job, User } from "./api";

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
  footprints: Record<string, Footprint>;
  visibleLayerIds: Record<string, boolean>;
  labelLayerIds: Record<string, boolean>;
  baseLayerId?: string;
  overlayLayerId?: string;
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
  toggleFootprint: (footprint: Footprint) => void;
  clearFootprints: () => void;
  toggleLayer: (layerId: string) => void;
  setLayerVisible: (layerId: string, visible: boolean) => void;
  toggleLayerLabels: (layerId: string) => void;
  setBaseLayer: (layerId?: string) => void;
  setOverlayLayer: (layerId?: string) => void;
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
  visibleLayerIds: {},
  labelLayerIds: {},
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
  toggleLayer: (layerId) =>
    set((state) => ({
      visibleLayerIds: {
        ...state.visibleLayerIds,
        [layerId]: !state.visibleLayerIds[layerId],
      },
    })),
  setLayerVisible: (layerId, visible) =>
    set((state) => ({
      visibleLayerIds: {
        ...state.visibleLayerIds,
        [layerId]: visible,
      },
    })),
  toggleLayerLabels: (layerId) =>
    set((state) => ({
      labelLayerIds: {
        ...state.labelLayerIds,
        [layerId]: !state.labelLayerIds[layerId],
      },
    })),
  setBaseLayer: (baseLayerId) => set({ baseLayerId }),
  setOverlayLayer: (overlayLayerId) => set({ overlayLayerId }),
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
