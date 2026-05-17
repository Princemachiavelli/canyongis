import { useEffect, useRef } from "react";
import "./leafletSetup";
import L from "leaflet";
import "@geoman-io/leaflet-geoman-free";
import { PMTiles, leafletRasterLayer } from "pmtiles";
import { GeoJSON, MapContainer, Rectangle, TileLayer, useMap } from "react-leaflet";
import type { Bounds } from "./api";
import type { LayerDescriptor } from "./layers";
import { useAppStore } from "./state";

function leafletBounds(bounds: [number, number, number, number]): L.LatLngBoundsExpression {
  return [
    [bounds[1], bounds[0]],
    [bounds[3], bounds[2]],
  ];
}

function toLeafletBounds(b: Bounds): L.LatLngBoundsExpression {
  return [
    [b.south, b.west],
    [b.north, b.east],
  ];
}

function PanController() {
  const map = useMap();
  const panRequest = useAppStore((state) => state.panRequest);

  useEffect(() => {
    if (!panRequest) return;
    map.fitBounds(toLeafletBounds(panRequest.bounds), { padding: [24, 24] });
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
      rotateMode: false,
    });

    const updateFromLayer = (layer: L.Layer) => {
      if ("getBounds" in layer) {
        const b = (layer as L.Rectangle | L.Polygon).getBounds();
        setBounds({
          west: b.getWest(),
          south: b.getSouth(),
          east: b.getEast(),
          north: b.getNorth(),
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
    const layer = L.rectangle(toLeafletBounds(bounds), {
      color: "#00bcd4",
      weight: 2,
      fillOpacity: 0.05,
    }).addTo(map);
    layer.pm.enable();
    selectionLayer.current = layer;
  }, [bounds, map]);

  return null;
}

function RasterLayer({ descriptor, opacity }: { descriptor: LayerDescriptor; opacity: number }) {
  const map = useMap();
  const layerRef = useRef<L.Layer | undefined>();
  const blend = descriptor.blend;
  const sourceType = descriptor.source?.type;
  const sourceUrl = descriptor.source?.url;
  const minZoom = descriptor.source?.min_zoom;
  const maxZoom = descriptor.source?.max_zoom;
  const boundsKey = descriptor.bounds_wgs84?.join(",");

  useEffect(() => {
    if (!sourceUrl) return;
    let layer: L.Layer;
    if (sourceType === "pmtiles") {
      const source = new PMTiles(sourceUrl);
      const tileLayer = leafletRasterLayer(source, {
        opacity,
        minZoom,
        maxNativeZoom: maxZoom,
        maxZoom: 22,
      });
      tileLayer.on("add", () => {
        const el = tileLayer.getContainer();
        if (el) {
          if (blend) el.style.mixBlendMode = blend;
          el.style.opacity = String(opacity);
        }
      });
      layer = tileLayer;
    } else if (sourceType === "image" && descriptor.bounds_wgs84) {
      const imgLayer = L.imageOverlay(sourceUrl, leafletBounds(descriptor.bounds_wgs84), {
        opacity,
      });
      if (blend) {
        imgLayer.on("add", () => {
          const el = imgLayer.getElement();
          if (el) el.style.mixBlendMode = blend;
        });
      }
      layer = imgLayer;
    } else {
      return;
    }
    layer.addTo(map);
    layerRef.current = layer;
    return () => {
      layerRef.current = undefined;
      layer.removeFrom(map);
    };
  }, [map, sourceType, sourceUrl, minZoom, maxZoom, blend, boundsKey]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    if (sourceType === "pmtiles") {
      const el = (layer as L.GridLayer).getContainer?.();
      if (el) el.style.opacity = String(opacity);
    } else if ("setOpacity" in layer) {
      (layer as L.ImageOverlay).setOpacity(opacity);
    }
  }, [opacity, sourceType]);

  return null;
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

function kmlFeatureLabel(
  feature: KmlFeature,
  fallbackName: string,
  fallbackCategory: string,
): string {
  const name = featureName(feature, fallbackName);
  const category = featureCategory(feature, fallbackCategory);
  const length = formatLength(lineLengthMeters(feature));
  return [name, length, category].filter(Boolean).join(" - ");
}

function kmlFeaturePopup(
  feature: KmlFeature,
  fallbackName: string,
  fallbackCategory: string,
): string {
  const name = escapeHtml(featureName(feature, fallbackName));
  const category = escapeHtml(featureCategory(feature, fallbackCategory));
  const length = formatLength(lineLengthMeters(feature));
  return [
    `<strong>${name}</strong>`,
    length ? `<div>Length: ${escapeHtml(length)}</div>` : "",
    `<div>Type: ${category}</div>`,
  ].join("");
}

function bindKmlFeature(
  sourceJobId: string,
  fallbackName: string,
  fallbackCategory: string,
  showLabels: boolean,
): (feature: KmlFeature, layer: L.Layer) => void {
  const setSelectedKmlTrack = useAppStore.getState().setSelectedKmlTrack;
  return (feature, layer) => {
    const label = kmlFeatureLabel(feature, fallbackName, fallbackCategory);
    layer.bindPopup(kmlFeaturePopup(feature, fallbackName, fallbackCategory));
    layer.bindTooltip(label, {
      className: showLabels ? "kmlLabel" : "kmlHoverLabel",
      direction: "top",
      permanent: showLabels,
      sticky: !showLabels,
    });
    if (fallbackCategory !== "point") {
      layer.on("click", () => {
        const rawId = feature.id ?? feature.properties?.id ?? 0;
        const trackIndex = Number.isFinite(Number(rawId)) ? Number(rawId) : 0;
        setSelectedKmlTrack({
          kmlJobId: sourceJobId,
          category: fallbackCategory as "descent" | "approach" | "unclassified",
          trackIndex,
          name: featureName(feature, fallbackName),
        });
      });
    }
  };
}

function VectorKmlLayer({ descriptor }: { descriptor: LayerDescriptor }) {
  const showLabels = Boolean(descriptor.showLabels);
  const sourceJobId = descriptor.sourceJobId ?? descriptor.id;
  const name = descriptor.fallbackName ?? descriptor.label;
  return (
    <>
      {descriptor.unclassified_geojson && (
        <GeoJSON
          key={`${descriptor.id}:unclassified:${showLabels}`}
          data={descriptor.unclassified_geojson}
          style={{ color: "#59656d", weight: 3, opacity: 0.85, dashArray: "4 4" }}
          onEachFeature={bindKmlFeature(sourceJobId, name, "unclassified", showLabels)}
        />
      )}
      {descriptor.approach_geojson && (
        <GeoJSON
          key={`${descriptor.id}:approach:${showLabels}`}
          data={descriptor.approach_geojson}
          style={{ color: "#2563eb", weight: 3, opacity: 0.9, dashArray: "8 4" }}
          onEachFeature={bindKmlFeature(sourceJobId, name, "approach", showLabels)}
        />
      )}
      {descriptor.descent_geojson && (
        <GeoJSON
          key={`${descriptor.id}:descent:${showLabels}`}
          data={descriptor.descent_geojson}
          style={{ color: "#d33f2f", weight: 4, opacity: 0.95 }}
          onEachFeature={bindKmlFeature(sourceJobId, name, "descent", showLabels)}
        />
      )}
      {descriptor.points_geojson && (
        <GeoJSON
          key={`${descriptor.id}:points:${showLabels}`}
          data={descriptor.points_geojson}
          onEachFeature={bindKmlFeature(sourceJobId, name, "point", showLabels)}
          pointToLayer={(_feature, latlng) =>
            L.circleMarker(latlng, {
              radius: 5,
              color: "#5d3b00",
              weight: 1,
              fillColor: "#f5b82e",
              fillOpacity: 0.95,
            })
          }
        />
      )}
    </>
  );
}

function PointEventsLayer({ descriptor }: { descriptor: LayerDescriptor }) {
  const data = descriptor.geojson;
  if (!data) return null;
  return (
    <GeoJSON
      key={`${descriptor.id}:point-events`}
      data={data}
      onEachFeature={(feature: KmlFeature, layer) => {
        const props = feature.properties ?? {};
        const drop = typeof props.drop_m === "number" ? `${props.drop_m.toFixed(1)} m` : "unknown";
        const slope =
          typeof props.slope === "number" ? `${Math.round(props.slope * 100)}%` : "unknown";
        layer.bindPopup(
          `<strong>${escapeHtml(descriptor.label)}</strong><div>Drop: ${drop}</div><div>Slope: ${slope}</div>`,
        );
        layer.bindTooltip(`${descriptor.label} ${drop}`, {
          className: "kmlHoverLabel",
          direction: "top",
          sticky: true,
        });
      }}
      pointToLayer={(_feature, latlng) =>
        L.circleMarker(latlng, {
          radius: 6,
          color: "#111827",
          weight: 2,
          fillColor: "#ffcc00",
          fillOpacity: 0.95,
        })
      }
    />
  );
}

function VectorGeoJsonLayer({ descriptor }: { descriptor: LayerDescriptor }) {
  const data = descriptor.geojson;
  if (!data) return null;
  return (
    <GeoJSON
      key={`${descriptor.id}:vector-geojson`}
      data={data}
      style={{ color: "#2563eb", weight: 3, opacity: descriptor.opacity ?? 0.9 }}
    />
  );
}

function FootprintLayer({ descriptor }: { descriptor: LayerDescriptor }) {
  if (!descriptor.bounds_wgs84) return null;
  return (
    <Rectangle
      bounds={leafletBounds(descriptor.bounds_wgs84)}
      pathOptions={{
        color: "#5b6e79",
        weight: 1,
        fillOpacity: descriptor.opacity ?? 0.04,
        dashArray: "4 4",
      }}
    />
  );
}

function LayerRenderer({ descriptor }: { descriptor: LayerDescriptor }) {
  const hillshadeOpacity = useAppStore((s) => s.hillshadeOpacity);
  const overlayOpacity = useAppStore((s) => s.overlayOpacity);
  switch (descriptor.kind) {
    case "base-raster":
      return <RasterLayer descriptor={descriptor} opacity={hillshadeOpacity} />;
    case "raster-overlay":
      return <RasterLayer descriptor={descriptor} opacity={overlayOpacity} />;
    case "vector-kml":
      return <VectorKmlLayer descriptor={descriptor} />;
    case "point-events":
      return <PointEventsLayer descriptor={descriptor} />;
    case "vector-geojson":
      return <VectorGeoJsonLayer descriptor={descriptor} />;
    case "footprint":
      return <FootprintLayer descriptor={descriptor} />;
    default:
      return null;
  }
}

export function MapView({ layers = [] }: { layers?: LayerDescriptor[] }) {
  const osmOpacity = useAppStore((state) => state.osmOpacity);
  const esriOpacity = useAppStore((state) => state.esriOpacity);

  return (
    <MapContainer
      center={[37.3, -112.9]}
      zoom={10}
      maxZoom={22}
      preferCanvas
      className="map"
    >
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        opacity={osmOpacity}
        maxNativeZoom={19}
        maxZoom={22}
      />
      <TileLayer
        attribution="Tiles &copy; Esri"
        url="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
        opacity={esriOpacity}
        maxNativeZoom={19}
        maxZoom={22}
      />
      <MapControls />
      <PanController />
      {layers.map((descriptor) => (
        <LayerRenderer key={descriptor.id} descriptor={descriptor} />
      ))}
    </MapContainer>
  );
}
