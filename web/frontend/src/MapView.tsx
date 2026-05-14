import { useEffect, useRef } from "react";
import "./leafletSetup";
import L from "leaflet";
import "@geoman-io/leaflet-geoman-free";
import { PMTiles, leafletRasterLayer } from "pmtiles";
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
  const bounds = useAppStore((state) => state.bounds);
  const setBounds = useAppStore((state) => state.setBounds);
  const selectionLayer = useRef<L.Rectangle | L.Polygon | undefined>();

  useEffect(() => {
    map.pm.addControls({
      position: "topleft",
      drawMarker: false,
      drawCircle: false,
      drawCircleMarker: false,
      drawPolyline: false,
      drawPolygon: false,
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

    map.on("pm:create", (event) => {
      if (selectionLayer.current && selectionLayer.current !== event.layer) {
        selectionLayer.current.removeFrom(map);
      }
      selectionLayer.current = event.layer as L.Rectangle | L.Polygon;
      updateFromLayer(event.layer);
    });
    map.on("pm:edit", (event) => {
      event.layers.eachLayer((layer: L.Layer) => updateFromLayer(layer));
    });
    map.on("pm:remove", (event) => {
      if (!selectionLayer.current || selectionLayer.current === event.layer) {
        selectionLayer.current = undefined;
        setBounds(undefined);
      }
    });
    return () => {
      map.pm.removeControls();
      selectionLayer.current?.removeFrom(map);
      selectionLayer.current = undefined;
    };
  }, [map, setBounds]);

  useEffect(() => {
    if (selectionLayer.current) {
      selectionLayer.current.removeFrom(map);
      selectionLayer.current = undefined;
    }
    if (!bounds) return;
    const layer = L.rectangle(leafletBounds(bounds), {
      color: "#00bcd4",
      weight: 2,
      fillOpacity: 0.05
    }).addTo(map);
    layer.pm.enable();
    selectionLayer.current = layer;
  }, [bounds, map]);

  return null;
}

function HillshadeBaseLayer() {
  const map = useMap();
  const hillshade = useAppStore((state) => state.activeHillshade);

  useEffect(() => {
    if (!hillshade) return;
    if (hillshade.tileArchiveUrl) {
      const source = new PMTiles(hillshade.tileArchiveUrl);
      const layer = leafletRasterLayer(source, {
        opacity: 1,
        minZoom: hillshade.tileMinZoom,
        maxNativeZoom: hillshade.tileMaxZoom,
        maxZoom: 22,
      });
      layer.on("add", () => {
        const el = layer.getContainer();
        if (el) {
          el.style.mixBlendMode = "multiply";
          el.style.opacity = "0.72";
        }
      });
      layer.addTo(map);
      return () => layer.removeFrom(map);
    }
    const layer = L.imageOverlay(hillshade.url, leafletBounds(hillshade.bounds), {
      opacity: 0.72,
    });
    layer.addTo(map);
    return () => layer.removeFrom(map);
  }, [map, hillshade?.id, hillshade?.url, hillshade?.tileArchiveUrl,
      hillshade?.tileMinZoom, hillshade?.tileMaxZoom, hillshade?.bounds]);

  return null;
}

function PmtilesOverlay({ multiply }: { multiply?: boolean }) {
  const map = useMap();
  const overlay = useAppStore((state) => state.overlay);

  useEffect(() => {
    if (!overlay?.tileArchiveUrl) return;
    const source = new PMTiles(overlay.tileArchiveUrl);
    const layer = leafletRasterLayer(source, {
      opacity: 0.72,
      minZoom: overlay.tileMinZoom,
      maxNativeZoom: overlay.tileMaxZoom,
      maxZoom: 22
    });
    layer.addTo(map);
    if (multiply) {
      layer.on("add", () => {
        const el = layer.getContainer();
        if (el) el.style.mixBlendMode = "multiply";
      });
    }
    return () => {
      layer.removeFrom(map);
    };
  }, [map, overlay?.id, overlay?.tileArchiveUrl, overlay?.tileMinZoom, overlay?.tileMaxZoom]);

  return null;
}

function geojsonResult(job: Job, field: string) {
  const value = job.result?.[field];
  if (!value || typeof value !== "object") return undefined;
  return value as GeoJSON.GeoJsonObject;
}

type KmlFeature = {
  id?: string | number;
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
  jobId: string,
  fallbackName: string,
  fallbackCategory: string,
  showLabels: boolean
): (feature: KmlFeature, layer: L.Layer) => void {
  const setSelectedKmlTrack = useAppStore.getState().setSelectedKmlTrack;
  return (feature, layer) => {
    const label = kmlFeatureLabel(feature, fallbackName, fallbackCategory);
    layer.bindPopup(kmlFeaturePopup(feature, fallbackName, fallbackCategory));
    layer.bindTooltip(label, {
      className: showLabels ? "kmlLabel" : "kmlHoverLabel",
      direction: "top",
      permanent: showLabels,
      sticky: !showLabels
    });
    if (fallbackCategory !== "point") {
      layer.on("click", () => {
        const rawId = feature.id ?? feature.properties?.id ?? 0;
        const trackIndex = Number.isFinite(Number(rawId)) ? Number(rawId) : 0;
        setSelectedKmlTrack({
          kmlJobId: jobId,
          category: fallbackCategory as "descent" | "approach" | "unclassified",
          trackIndex,
          name: featureName(feature, fallbackName)
        });
      });
    }
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
          onEachFeature={bindKmlFeature(job.id, name, "unclassified", showLabels)}
        />
      )}
      {approach && (
        <GeoJSON
          key={`${job.id}:approach:${showLabels}`}
          data={approach}
          style={{ color: "#2563eb", weight: 3, opacity: 0.9, dashArray: "8 4" }}
          onEachFeature={bindKmlFeature(job.id, name, "approach", showLabels)}
        />
      )}
      {descent && (
        <GeoJSON
          key={`${job.id}:descent:${showLabels}`}
          data={descent}
          style={{ color: "#d33f2f", weight: 4, opacity: 0.95 }}
          onEachFeature={bindKmlFeature(job.id, name, "descent", showLabels)}
        />
      )}
      {points && (
        <GeoJSON
          key={`${job.id}:points:${showLabels}`}
          data={points}
          onEachFeature={bindKmlFeature(job.id, name, "point", showLabels)}
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

function ProfileRappelsLayer({ job }: { job: Job }) {
  const points = geojsonResult(job, "rappel_candidates_geojson");
  if (!points) return null;
  return (
    <GeoJSON
      key={`${job.id}:profile-rappels`}
      data={points}
      onEachFeature={(feature: KmlFeature, layer) => {
        const props = feature.properties ?? {};
        const drop = typeof props.drop_m === "number" ? `${props.drop_m.toFixed(1)} m` : "unknown";
        const slope = typeof props.slope === "number" ? `${Math.round(props.slope * 100)}%` : "unknown";
        layer.bindPopup(`<strong>Rappel candidate</strong><div>Drop: ${drop}</div><div>Slope: ${slope}</div>`);
        layer.bindTooltip(`rappel ${drop}`, {
          className: "kmlHoverLabel",
          direction: "top",
          sticky: true
        });
      }}
      pointToLayer={(_feature, latlng) =>
        L.circleMarker(latlng, {
          radius: 6,
          color: "#111827",
          weight: 2,
          fillColor: "#ffcc00",
          fillOpacity: 0.95
        })
      }
    />
  );
}

export function MapView({ kmlImports = [], profileJob }: { kmlImports?: Job[]; profileJob?: Job }) {
  const bounds = useAppStore((state) => state.bounds);
  const overlay = useAppStore((state) => state.overlay);
  const hillshade = useAppStore((state) => state.activeHillshade);
  const footprints = useAppStore((state) => state.footprints);
  const visibleKmls = useAppStore((state) => state.visibleKmls);
  const kmlLabels = useAppStore((state) => state.kmlLabels);

  return (
    <MapContainer center={[37.3, -112.9]} zoom={10} maxZoom={22} className="map">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        maxNativeZoom={19}
        maxZoom={22}
      />
      <TileLayer
        attribution="Tiles &copy; Esri"
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        opacity={0.35}
        maxNativeZoom={19}
        maxZoom={22}
      />
      <MapControls />
      <PanController />
      <HillshadeBaseLayer />
      {Object.values(footprints).map((fp) => (
        <Rectangle
          key={fp.id}
          bounds={leafletBounds(fp.bounds)}
          pathOptions={{ color: "#5b6e79", weight: 1, fillOpacity: 0.04, dashArray: "4 4" }}
        />
      ))}
      {overlay?.tileArchiveUrl ? (
        <PmtilesOverlay multiply={!!hillshade} />
      ) : (
        overlay && <ImageOverlay
          url={overlay.url}
          bounds={leafletBounds(overlay.bounds)}
          opacity={0.72}
          className={hillshade ? "multiplyBlend" : undefined}
        />
      )}
      {kmlImports
        .filter((job) => job.status === "succeeded" && visibleKmls[job.id])
        .map((job) => (
          <KmlImportLayer key={job.id} job={job} showLabels={Boolean(kmlLabels[job.id])} />
        ))}
      {profileJob?.status === "succeeded" && <ProfileRappelsLayer job={profileJob} />}
    </MapContainer>
  );
}
