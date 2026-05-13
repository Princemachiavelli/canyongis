import { useEffect } from "react";
import "./leafletSetup";
import L from "leaflet";
import "@geoman-io/leaflet-geoman-free";
import { GeoJSON, ImageOverlay, MapContainer, Rectangle, TileLayer, useMap } from "react-leaflet";
import type { Bounds, Job } from "./api";
import { useAppStore } from "./state";

function leafletBounds(bounds: Bounds): L.LatLngBoundsExpression {
  return [
    [bounds.south, bounds.west],
    [bounds.north, bounds.east]
  ];
}

function PanController() {
  const map = useMap();
  const panRequest = useAppStore((state) => state.panRequest);

  useEffect(() => {
    if (!panRequest) return;
    map.fitBounds(leafletBounds(panRequest.bounds), { padding: [24, 24] });
  }, [map, panRequest]);

  return null;
}

function MapControls() {
  const map = useMap();
  const setBounds = useAppStore((state) => state.setBounds);

  useEffect(() => {
    map.pm.addControls({
      position: "topleft",
      drawMarker: false,
      drawCircle: false,
      drawCircleMarker: false,
      drawPolyline: false,
      drawPolygon: true,
      drawRectangle: true,
      editMode: true,
      dragMode: true,
      cutPolygon: false,
      removalMode: true,
      rotateMode: false
    });

    const updateFromLayer = (layer: L.Layer) => {
      if ("getBounds" in layer) {
        const b = (layer as L.Rectangle | L.Polygon).getBounds();
        setBounds({
          west: b.getWest(),
          south: b.getSouth(),
          east: b.getEast(),
          north: b.getNorth()
        });
      }
    };

    map.on("pm:create", (event) => updateFromLayer(event.layer));
    map.on("pm:edit", (event) => {
      event.layers.eachLayer((layer: L.Layer) => updateFromLayer(layer));
    });
    map.on("pm:remove", () => setBounds(undefined));
    return () => {
      map.pm.removeControls();
    };
  }, [map, setBounds]);

  return null;
}

function geojsonResult(job: Job, field: string) {
  const value = job.result?.[field];
  if (!value || typeof value !== "object") return undefined;
  return value as GeoJSON.GeoJsonObject;
}

type KmlFeature = {
  type?: string;
  properties?: Record<string, unknown> | null;
  geometry?: {
    type?: string;
    coordinates?: unknown;
  } | null;
};

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (ch) => {
    switch (ch) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return ch;
    }
  });
}

function featureName(feature: KmlFeature, fallback: string): string {
  const name = feature.properties?.name;
  return typeof name === "string" && name.trim() ? name.trim() : fallback;
}

function featureCategory(feature: KmlFeature, fallback: string): string {
  const category = feature.properties?.category;
  return typeof category === "string" && category.trim() ? category.trim() : fallback;
}

function formatLength(meters?: number): string | undefined {
  if (meters === undefined || !Number.isFinite(meters)) return undefined;
  if (meters < 1000) return `${Math.round(meters)} m`;
  return `${(meters / 1000).toFixed(meters < 10000 ? 2 : 1)} km`;
}

function lineLengthMeters(feature: KmlFeature): number | undefined {
  const geometry = feature.geometry;
  if (!geometry) return undefined;

  const segmentLength = (coords: unknown): number => {
    if (!Array.isArray(coords)) return 0;
    let total = 0;
    for (let i = 1; i < coords.length; i += 1) {
      const prev = coords[i - 1];
      const next = coords[i];
      if (!Array.isArray(prev) || !Array.isArray(next)) continue;
      const [lng1, lat1] = prev;
      const [lng2, lat2] = next;
      if (
        typeof lng1 !== "number" ||
        typeof lat1 !== "number" ||
        typeof lng2 !== "number" ||
        typeof lat2 !== "number"
      ) {
        continue;
      }
      total += L.latLng(lat1, lng1).distanceTo(L.latLng(lat2, lng2));
    }
    return total;
  };

  if (geometry.type === "LineString") return segmentLength(geometry.coordinates);
  if (geometry.type === "MultiLineString" && Array.isArray(geometry.coordinates)) {
    return geometry.coordinates.reduce<number>((sum, coords) => sum + segmentLength(coords), 0);
  }
  return undefined;
}

function kmlFeatureLabel(feature: KmlFeature, fallbackName: string, fallbackCategory: string): string {
  const name = featureName(feature, fallbackName);
  const category = featureCategory(feature, fallbackCategory);
  const length = formatLength(lineLengthMeters(feature));
  return [name, length, category].filter(Boolean).join(" - ");
}

function kmlFeaturePopup(feature: KmlFeature, fallbackName: string, fallbackCategory: string): string {
  const name = escapeHtml(featureName(feature, fallbackName));
  const category = escapeHtml(featureCategory(feature, fallbackCategory));
  const length = formatLength(lineLengthMeters(feature));
  return [
    `<strong>${name}</strong>`,
    length ? `<div>Length: ${escapeHtml(length)}</div>` : "",
    `<div>Type: ${category}</div>`
  ].join("");
}

function bindKmlFeature(
  fallbackName: string,
  fallbackCategory: string,
  showLabels: boolean
): (feature: KmlFeature, layer: L.Layer) => void {
  return (feature, layer) => {
    const label = kmlFeatureLabel(feature, fallbackName, fallbackCategory);
    layer.bindPopup(kmlFeaturePopup(feature, fallbackName, fallbackCategory));
    layer.bindTooltip(label, {
      className: showLabels ? "kmlLabel" : "kmlHoverLabel",
      direction: "top",
      permanent: showLabels,
      sticky: !showLabels
    });
  };
}

function KmlImportLayer({ job, showLabels }: { job: Job; showLabels: boolean }) {
  const descent = geojsonResult(job, "descent_geojson");
  const approach = geojsonResult(job, "approach_geojson");
  const unclassified = geojsonResult(job, "unclassified_geojson");
  const points = geojsonResult(job, "points_geojson");
  const name = (job.result?.name as string | undefined) ?? (job.params.name as string | undefined) ?? job.id;

  return (
    <>
      {unclassified && (
        <GeoJSON
          key={`${job.id}:unclassified:${showLabels}`}
          data={unclassified}
          style={{ color: "#59656d", weight: 3, opacity: 0.85, dashArray: "4 4" }}
          onEachFeature={bindKmlFeature(name, "unclassified", showLabels)}
        />
      )}
      {approach && (
        <GeoJSON
          key={`${job.id}:approach:${showLabels}`}
          data={approach}
          style={{ color: "#2563eb", weight: 3, opacity: 0.9, dashArray: "8 4" }}
          onEachFeature={bindKmlFeature(name, "approach", showLabels)}
        />
      )}
      {descent && (
        <GeoJSON
          key={`${job.id}:descent:${showLabels}`}
          data={descent}
          style={{ color: "#d33f2f", weight: 4, opacity: 0.95 }}
          onEachFeature={bindKmlFeature(name, "descent", showLabels)}
        />
      )}
      {points && (
        <GeoJSON
          key={`${job.id}:points:${showLabels}`}
          data={points}
          onEachFeature={bindKmlFeature(name, "point", showLabels)}
          pointToLayer={(_feature, latlng) =>
            L.circleMarker(latlng, {
              radius: 5,
              color: "#5d3b00",
              weight: 1,
              fillColor: "#f5b82e",
              fillOpacity: 0.95
            })
          }
        />
      )}
    </>
  );
}

export function MapView({ kmlImports = [] }: { kmlImports?: Job[] }) {
  const bounds = useAppStore((state) => state.bounds);
  const overlay = useAppStore((state) => state.overlay);
  const footprints = useAppStore((state) => state.footprints);
  const visibleKmls = useAppStore((state) => state.visibleKmls);
  const kmlLabels = useAppStore((state) => state.kmlLabels);

  return (
    <MapContainer center={[37.3, -112.9]} zoom={10} className="map">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <TileLayer
        attribution="Tiles &copy; Esri"
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        opacity={0.35}
      />
      <MapControls />
      <PanController />
      {Object.values(footprints).map((fp) => (
        <Rectangle
          key={fp.id}
          bounds={leafletBounds(fp.bounds)}
          pathOptions={{ color: "#5b6e79", weight: 1, fillOpacity: 0.04, dashArray: "4 4" }}
        />
      ))}
      {bounds && <Rectangle bounds={leafletBounds(bounds)} pathOptions={{ color: "#00bcd4", weight: 2 }} />}
      {overlay && <ImageOverlay url={overlay.url} bounds={leafletBounds(overlay.bounds)} opacity={0.72} />}
      {kmlImports
        .filter((job) => job.status === "succeeded" && visibleKmls[job.id])
        .map((job) => (
          <KmlImportLayer key={job.id} job={job} showLabels={Boolean(kmlLabels[job.id])} />
        ))}
    </MapContainer>
  );
}
