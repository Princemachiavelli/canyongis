import type { Job } from "./api";

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
  blend?: string;
  bounds_wgs84?: [number, number, number, number];
  source?: LayerSource;
  role?: string;
  sourceJobId?: string;
  fallbackName?: string;
  descent_geojson?: GeoJSON.GeoJsonObject;
  approach_geojson?: GeoJSON.GeoJsonObject;
  unclassified_geojson?: GeoJSON.GeoJsonObject;
  points_geojson?: GeoJSON.GeoJsonObject;
  geojson?: GeoJSON.GeoJsonObject;
  showLabels?: boolean;
};

export function namespacedLayerId(jobId: string, layerKey: string): string {
  return `${jobId}:${layerKey}`;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function jobName(job: Job): string {
  const fromResult = job.result?.name;
  if (typeof fromResult === "string" && fromResult) return fromResult;
  const fromParams = (job.params as { name?: unknown }).name;
  if (typeof fromParams === "string" && fromParams) return fromParams;
  return job.id;
}

function toBounds(value: unknown): [number, number, number, number] | undefined {
  if (!Array.isArray(value) || value.length !== 4) return undefined;
  const nums = value.map((v) => Number(v));
  if (nums.some((n) => !Number.isFinite(n))) return undefined;
  return [nums[0], nums[1], nums[2], nums[3]];
}

function geojsonField(layer: Record<string, unknown>, field: string): GeoJSON.GeoJsonObject | undefined {
  const v = layer[field];
  if (!v || typeof v !== "object") return undefined;
  return v as GeoJSON.GeoJsonObject;
}

export function descriptorsFromJob(job: Job): LayerDescriptor[] {
  if (job.status !== "succeeded" || !job.result) return [];
  const layers = job.result.layers;
  if (!Array.isArray(layers)) return [];
  const out: LayerDescriptor[] = [];
  for (const raw of layers) {
    if (!isObject(raw)) continue;
    const id = typeof raw.id === "string" ? raw.id : typeof raw.key === "string" ? raw.key : undefined;
    const kind = typeof raw.kind === "string" ? (raw.kind as LayerKind) : undefined;
    if (!id || !kind) continue;
    const label = typeof raw.label === "string" ? raw.label : id;
    const source = isObject(raw.source) ? (raw.source as LayerSource) : undefined;
    const opacity = typeof raw.opacity === "number" ? raw.opacity : undefined;
    const blend = typeof raw.blend === "string" ? raw.blend : undefined;
    const role = typeof raw.role === "string" ? raw.role : undefined;
    const bounds = toBounds(raw.bounds_wgs84);
    const desc: LayerDescriptor = {
      id: namespacedLayerId(job.id, id),
      label,
      kind,
      opacity,
      blend,
      bounds_wgs84: bounds,
      source,
      role,
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
      const data = geojsonField(raw, "geojson");
      if (data) desc.geojson = data;
    }
    out.push(desc);
  }
  return out;
}

export function allDescriptorsFromJobs(jobs: Job[]): LayerDescriptor[] {
  const out: LayerDescriptor[] = [];
  for (const job of jobs) out.push(...descriptorsFromJob(job));
  return out;
}

export function descriptorJobId(layerId: string): string | undefined {
  const idx = layerId.indexOf(":");
  return idx > 0 ? layerId.slice(0, idx) : undefined;
}
