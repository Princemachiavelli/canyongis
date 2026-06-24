// Job result → GeoLibre layers. Port of web/frontend/src/layers.ts descriptor
// synthesis, with rendering handed to GeoLibre: vectors go through
// addGeoJsonLayer (store-managed, styleable in the Layers panel), rasters are
// added as native MapLibre sources/layers and mirrored into the store via
// registerExternalNativeLayer.

import type { Job } from "./api";
import type { CanyonGisClient } from "./api";
import type { FeatureCollection, GeoJsonObject, GeoLibreAppAPI } from "./geolibre";

export type LayerKind =
  | "base-raster"
  | "raster-overlay"
  | "vector-kml"
  | "vector-geojson"
  | "point-events"
  | "footprint";

export type LayerSource = {
  type: "image" | "pmtiles" | "kml" | "geojson" | "metadata";
  url?: string;
  path?: string;
  min_zoom?: number;
  max_zoom?: number;
};

export type LayerDescriptor = {
  id: string;
  label: string;
  kind: LayerKind;
  opacity?: number;
  bounds_wgs84?: [number, number, number, number];
  source?: LayerSource;
  sourceJobId?: string;
  fallbackName?: string;
  descent_geojson?: GeoJsonObject;
  approach_geojson?: GeoJsonObject;
  unclassified_geojson?: GeoJsonObject;
  points_geojson?: GeoJsonObject;
  geojson?: GeoJsonObject;
};

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function jobName(job: Job): string {
  const fromResult = job.result?.name;
  if (typeof fromResult === "string" && fromResult) return fromResult;
  const fromParams = (job.params as { name?: unknown }).name;
  if (typeof fromParams === "string" && fromParams) return fromParams;
  return job.id;
}

export function toBounds(value: unknown): [number, number, number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 4) return undefined;
  const nums = value.map((v) => Number(v));
  if (nums.some((n) => !Number.isFinite(n))) return undefined;
  return [nums[0], nums[1], nums[2], nums[3]];
}

function geojsonField(layer: Record<string, unknown>, field: string): GeoJsonObject | undefined {
  const v = layer[field];
  if (!v || typeof v !== "object") return undefined;
  return v as GeoJsonObject;
}

export function descriptorsFromJob(job: Job): LayerDescriptor[] {
  if (job.status !== "succeeded" || !job.result) return [];
  const layers = job.result.layers;
  if (!Array.isArray(layers)) return [];
  const out: LayerDescriptor[] = [];
  for (const raw of layers) {
    if (!isObject(raw)) continue;
    const id =
      typeof raw.id === "string" ? raw.id : typeof raw.key === "string" ? raw.key : undefined;
    const kind = typeof raw.kind === "string" ? (raw.kind as LayerKind) : undefined;
    if (!id || !kind) continue;
    const desc: LayerDescriptor = {
      id: `${job.id}:${id}`,
      label: typeof raw.label === "string" ? raw.label : id,
      kind,
      opacity: typeof raw.opacity === "number" ? raw.opacity : undefined,
      bounds_wgs84: toBounds(raw.bounds_wgs84),
      source: isObject(raw.source) ? (raw.source as LayerSource) : undefined,
      sourceJobId: job.id,
      fallbackName: jobName(job),
    };
    if (kind === "vector-kml") {
      desc.descent_geojson = geojsonField(raw, "descent_geojson");
      desc.approach_geojson = geojsonField(raw, "approach_geojson");
      desc.unclassified_geojson = geojsonField(raw, "unclassified_geojson");
      desc.points_geojson = geojsonField(raw, "points_geojson");
    }
    if (kind === "point-events" || kind === "vector-geojson") {
      desc.geojson = geojsonField(raw, "geojson");
    }
    out.push(desc);
  }
  return out;
}

function asFeatureCollection(data: GeoJsonObject): FeatureCollection | undefined {
  if (data.type === "FeatureCollection" && Array.isArray(data.features)) {
    const fc = data as unknown as FeatureCollection;
    return fc.features.length > 0 ? fc : undefined;
  }
  if (data.type === "Feature") return { type: "FeatureCollection", features: [data] };
  return undefined;
}

function nativeIds(descriptorId: string): { sourceId: string; layerId: string } {
  const sourceId = `canyongis:${descriptorId}`;
  return { sourceId, layerId: `${sourceId}:layer` };
}

function addRasterDescriptor(
  app: GeoLibreAppAPI,
  client: CanyonGisClient,
  desc: LayerDescriptor,
  log: (msg: string) => void,
): void {
  const map = app.getMap?.();
  if (!map) {
    log(`${desc.label}: map unavailable, raster layer skipped`);
    return;
  }
  if (!desc.source?.url) {
    log(`${desc.label}: no source URL, skipped`);
    return;
  }
  const { sourceId, layerId } = nativeIds(desc.id);
  if (map.getLayer(layerId)) return;
  const url = client.absUrl(desc.source.url);
  try {
    if (desc.source.type === "image") {
      if (!desc.bounds_wgs84) {
        log(`${desc.label}: image layer missing bounds, skipped`);
        return;
      }
      const [w, s, e, n] = desc.bounds_wgs84;
      map.addSource(sourceId, {
        type: "image",
        url,
        coordinates: [
          [w, n],
          [e, n],
          [e, s],
          [w, s],
        ],
      });
    } else if (desc.source.type === "pmtiles") {
      // Relies on the pmtiles:// protocol the GeoLibre host registers for its
      // own PMTiles support. If unavailable, tiles fail to load (visible in
      // the browser console) but the app keeps working.
      map.addSource(sourceId, {
        type: "raster",
        url: `pmtiles://${url}`,
        tileSize: 256,
        ...(typeof desc.source.min_zoom === "number" ? { minzoom: desc.source.min_zoom } : {}),
        ...(typeof desc.source.max_zoom === "number" ? { maxzoom: desc.source.max_zoom } : {}),
      });
    } else {
      return;
    }
    map.addLayer({
      id: layerId,
      type: "raster",
      source: sourceId,
      paint: { "raster-opacity": desc.opacity ?? 0.85 },
    });
  } catch (error) {
    log(`${desc.label}: ${error instanceof Error ? error.message : String(error)}`);
    return;
  }
  app.registerExternalNativeLayer?.({
    id: desc.id,
    name: `${desc.fallbackName ?? desc.sourceJobId ?? "canyongis"} — ${desc.label}`,
    type: "raster",
    source: { type: "raster", url },
    nativeLayerIds: [layerId],
    sourceIds: [sourceId],
    opacity: desc.opacity ?? 0.85,
    metadata: {
      canyongisJobId: desc.sourceJobId,
      canyongisLayerId: desc.id,
      canyongisServer: client.baseUrl,
    },
    sourcePath: url,
  });
}

function addVectorDescriptor(
  app: GeoLibreAppAPI,
  desc: LayerDescriptor,
  log: (msg: string) => void,
): void {
  const base = `${desc.fallbackName ?? "canyongis"} — ${desc.label}`;
  const parts: Array<[string, GeoJsonObject | undefined]> = [];
  if (desc.kind === "vector-kml") {
    parts.push(
      [`${base} (descent)`, desc.descent_geojson],
      [`${base} (approach)`, desc.approach_geojson],
      [`${base} (tracks)`, desc.unclassified_geojson],
      [`${base} (points)`, desc.points_geojson],
    );
  } else {
    parts.push([base, desc.geojson]);
  }
  let added = 0;
  for (const [name, data] of parts) {
    if (!data) continue;
    const fc = asFeatureCollection(data);
    if (!fc) continue;
    app.addGeoJsonLayer(name, fc);
    added += 1;
  }
  if (added === 0) log(`${desc.label}: no vector features to add`);
}

/**
 * Add every renderable layer of a succeeded job to the GeoLibre map.
 * Descriptors already in `added` are skipped (GeoJSON layers are not
 * idempotent on the GeoLibre side, unlike our native raster layers), and
 * newly processed ids are recorded there. Returns the processed ids.
 */
export function addJobLayers(
  app: GeoLibreAppAPI,
  client: CanyonGisClient,
  job: Job,
  log: (msg: string) => void,
  added?: Set<string>,
): string[] {
  const processed: string[] = [];
  for (const desc of descriptorsFromJob(job)) {
    if (desc.kind === "footprint") continue;
    if (added?.has(desc.id)) continue;
    if (desc.kind === "base-raster" || desc.kind === "raster-overlay") {
      addRasterDescriptor(app, client, desc, log);
    } else {
      addVectorDescriptor(app, desc, log);
    }
    added?.add(desc.id);
    processed.push(desc.id);
  }
  return processed;
}

/** Best-effort bounds for zooming to a finished job. */
export function jobBounds(job: Job): [number, number, number, number] | undefined {
  const direct = toBounds(job.result?.bounds_wgs84);
  if (direct) return direct;
  for (const desc of descriptorsFromJob(job)) {
    if (desc.bounds_wgs84) return desc.bounds_wgs84;
  }
  return undefined;
}
