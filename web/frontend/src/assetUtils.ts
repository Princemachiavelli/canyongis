import type { Asset, Bounds, Job, JobSpec } from "./api";

export type CompatibleTool = { spec: JobSpec; inputKey: string };

export const ASSET_GROUP_ORDER = [
  "aoi",
  "dem",
  "route",
  "score",
  "spine",
  "event",
  "preview",
  "raster",
  "other",
];

export function assetGroup(kind: string): { key: string; label: string } {
  if (kind === "geom.bounds") return { key: "aoi", label: "AOIs" };
  if (kind === "raster.dem" || kind === "raster.hillshade") {
    return { key: "dem", label: "DEMs" };
  }
  if (kind.startsWith("vector.kml.route")) return { key: "route", label: "Routes" };
  if (kind.startsWith("raster.score")) return { key: "score", label: "Scores" };
  if (kind.startsWith("vector.kml.spine")) return { key: "spine", label: "Spines" };
  if (kind.includes("rappel") || kind.startsWith("vector.geojson.points")) {
    return { key: "event", label: "Events" };
  }
  if (
    kind.startsWith("preview.") ||
    kind === "pmtiles" ||
    kind === "preview_png" ||
    kind === "preview_webp" ||
    kind === "preview_jpg"
  ) {
    return { key: "preview", label: "Previews" };
  }
  if (kind.startsWith("raster.")) return { key: "raster", label: "Rasters" };
  return { key: "other", label: "Other" };
}

export function assetName(asset: Asset, job?: Job): string {
  const label = asset.metadata.label;
  if (typeof label === "string" && label) return label;
  const outputKey = asset.metadata.output_key;
  const jobName =
    (job?.result?.name as string | undefined) ?? (job?.params.name as string | undefined);
  if (jobName && typeof outputKey === "string") return `${jobName} ${outputKey}`;
  if (jobName) return jobName;
  if (asset.path) return asset.path.split("/").pop() ?? asset.id;
  return asset.id;
}

export function assetBounds(asset: Asset): Bounds | undefined {
  const arr = asset.bounds_wgs84;
  if (!arr || arr.length !== 4) return undefined;
  return { west: arr[0], south: arr[1], east: arr[2], north: arr[3] };
}

export function matchesInput(assetKind: string, inputKind: string): boolean {
  return assetKind === inputKind || assetKind.startsWith(`${inputKind}.`);
}

export function compatibleSpecs(asset: Asset, specs: JobSpec[]): CompatibleTool[] {
  const matches: CompatibleTool[] = [];
  for (const spec of specs) {
    const input = spec.inputs.find((inp) => matchesInput(asset.kind, inp.kind));
    if (input) matches.push({ spec, inputKey: input.key });
  }
  return matches;
}
