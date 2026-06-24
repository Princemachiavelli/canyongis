// Structural typings for the GeoLibre host plugin API (packages/plugins/src/types.ts).
// Declared locally so the bundle has zero dependencies — the loader fetches the
// entry as text and imports it from a blob URL, so nothing can be left external.

export type ControlPosition = "top-left" | "top-right" | "bottom-left" | "bottom-right";

export type FeatureCollection = {
  type: "FeatureCollection";
  features: unknown[];
};

export type GeoJsonObject = { type: string; [key: string]: unknown };

/** Minimal slice of maplibregl.Map the plugin drives through getMap(). */
export interface MapLike {
  addSource(id: string, spec: Record<string, unknown>): void;
  removeSource(id: string): void;
  getSource(id: string): unknown;
  addLayer(spec: Record<string, unknown>, beforeId?: string): void;
  removeLayer(id: string): void;
  getLayer(id: string): unknown;
  setPaintProperty(layerId: string, prop: string, value: unknown): void;
  getCanvas(): HTMLCanvasElement;
  dragPan: { enable(): void; disable(): void };
  on(type: string, listener: (ev: MapMouseEventLike) => void): void;
  off(type: string, listener: (ev: MapMouseEventLike) => void): void;
  fitBounds(
    bounds: [[number, number], [number, number]],
    options?: Record<string, unknown>,
  ): void;
}

export interface MapMouseEventLike {
  lngLat: { lng: number; lat: number };
  preventDefault?: () => void;
}

/** Structural maplibre IControl. */
export interface MapControl {
  onAdd(map: MapLike): HTMLElement;
  onRemove(map: MapLike): void;
}

export interface ExternalNativeLayerRegistration {
  id: string;
  name: string;
  type?: string;
  source?: Record<string, unknown>;
  geojson?: FeatureCollection;
  nativeLayerIds: string[];
  sourceIds?: string[];
  sourceId?: string;
  beforeId?: string;
  opacity?: number;
  style?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  sourcePath?: string;
}

export interface GeoLibreAppAPI {
  setBasemap: (styleUrl: string) => void;
  addGeoJsonLayer: (name: string, data: FeatureCollection, sourcePath?: string) => void;
  getActiveBasemap: () => string;
  onBasemapChange: (callback: (styleUrl: string) => void) => () => void;
  fetchArrayBuffer?: (url: string) => Promise<ArrayBuffer>;
  fitBounds?: (bounds: [number, number, number, number]) => void;
  getMap?: () => MapLike | null;
  registerExternalNativeLayer?: (layer: ExternalNativeLayerRegistration) => void;
  unregisterExternalNativeLayer?: (id: string) => void;
  addMapControl: (control: MapControl, position?: ControlPosition) => boolean;
  removeMapControl: (control: MapControl) => void;
}

export interface GeoLibrePlugin {
  id: string;
  name: string;
  version: string;
  urlParameterNames?: string[];
  activate: (app: GeoLibreAppAPI) => boolean | void;
  deactivate: (app: GeoLibreAppAPI) => void;
  handleUrlParameters?: (app: GeoLibreAppAPI, params: URLSearchParams) => void | Promise<void>;
  getMapControlPosition?: () => ControlPosition;
  setMapControlPosition?: (app: GeoLibreAppAPI, position: ControlPosition) => boolean | void;
  getProjectState?: () => unknown;
  applyProjectState?: (app: GeoLibreAppAPI, state: unknown) => boolean | void;
}
