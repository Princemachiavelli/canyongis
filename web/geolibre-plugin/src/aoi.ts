// Drag-rectangle AOI selection on the raw MapLibre map. The rectangle is an
// ephemeral plugin-owned source/layer pair (not registered in the GeoLibre
// store) — it is working state for the DEM build, not a data layer.

import type { Bounds } from "./api";
import type { MapLike, MapMouseEventLike } from "./geolibre";

const AOI_SOURCE = "canyongis-aoi";
const AOI_FILL = "canyongis-aoi-fill";
const AOI_LINE = "canyongis-aoi-line";

function aoiFeature(b: Bounds): Record<string, unknown> {
  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "Polygon",
      coordinates: [
        [
          [b.west, b.south],
          [b.east, b.south],
          [b.east, b.north],
          [b.west, b.north],
          [b.west, b.south],
        ],
      ],
    },
  };
}

export function showAoi(map: MapLike, bounds: Bounds): void {
  const data = { type: "FeatureCollection", features: [aoiFeature(bounds)] };
  const existing = map.getSource(AOI_SOURCE) as
    | { setData?: (d: unknown) => void }
    | undefined;
  if (existing?.setData) {
    existing.setData(data);
    return;
  }
  map.addSource(AOI_SOURCE, { type: "geojson", data });
  map.addLayer({
    id: AOI_FILL,
    type: "fill",
    source: AOI_SOURCE,
    paint: { "fill-color": "#2563eb", "fill-opacity": 0.08 },
  });
  map.addLayer({
    id: AOI_LINE,
    type: "line",
    source: AOI_SOURCE,
    paint: { "line-color": "#2563eb", "line-width": 2, "line-dasharray": [2, 1] },
  });
}

export function clearAoi(map: MapLike): void {
  for (const id of [AOI_FILL, AOI_LINE]) {
    try {
      if (map.getLayer(id)) map.removeLayer(id);
    } catch {
      // layer may already be gone (style reload)
    }
  }
  try {
    if (map.getSource(AOI_SOURCE)) map.removeSource(AOI_SOURCE);
  } catch {
    // ditto
  }
}

export type AoiDrawHandle = { cancel: () => void };

/**
 * One-shot rectangle draw: disables map panning, tracks mousedown→drag→mouseup,
 * renders the rectangle live, then restores panning and reports the bounds.
 */
export function startAoiDraw(map: MapLike, onDone: (bounds: Bounds) => void): AoiDrawHandle {
  let start: { lng: number; lat: number } | undefined;
  let finished = false;

  const canvas = map.getCanvas();
  const prevCursor = canvas.style.cursor;
  canvas.style.cursor = "crosshair";
  map.dragPan.disable();

  const boundsFrom = (a: { lng: number; lat: number }, b: { lng: number; lat: number }): Bounds => ({
    west: Math.min(a.lng, b.lng),
    south: Math.min(a.lat, b.lat),
    east: Math.max(a.lng, b.lng),
    north: Math.max(a.lat, b.lat),
  });

  const cleanup = () => {
    if (finished) return;
    finished = true;
    map.off("mousedown", onDown);
    map.off("mousemove", onMove);
    map.off("mouseup", onUp);
    canvas.style.cursor = prevCursor;
    map.dragPan.enable();
  };

  const onDown = (ev: MapMouseEventLike) => {
    ev.preventDefault?.();
    start = { lng: ev.lngLat.lng, lat: ev.lngLat.lat };
  };

  const onMove = (ev: MapMouseEventLike) => {
    if (!start) return;
    showAoi(map, boundsFrom(start, ev.lngLat));
  };

  const onUp = (ev: MapMouseEventLike) => {
    if (!start) return;
    const bounds = boundsFrom(start, ev.lngLat);
    cleanup();
    if (bounds.east - bounds.west < 1e-6 || bounds.north - bounds.south < 1e-6) {
      clearAoi(map);
      return;
    }
    showAoi(map, bounds);
    onDone(bounds);
  };

  map.on("mousedown", onDown);
  map.on("mousemove", onMove);
  map.on("mouseup", onUp);

  return { cancel: cleanup };
}
