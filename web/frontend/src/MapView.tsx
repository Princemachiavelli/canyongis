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

function KmlImportLayer({ job }: { job: Job }) {
  const descent = geojsonResult(job, "descent_geojson");
  const approach = geojsonResult(job, "approach_geojson");
  const unclassified = geojsonResult(job, "unclassified_geojson");
  const points = geojsonResult(job, "points_geojson");

  return (
    <>
      {unclassified && (
        <GeoJSON
          key={`${job.id}:unclassified`}
          data={unclassified}
          style={{ color: "#59656d", weight: 3, opacity: 0.85, dashArray: "4 4" }}
        />
      )}
      {approach && (
        <GeoJSON
          key={`${job.id}:approach`}
          data={approach}
          style={{ color: "#2563eb", weight: 3, opacity: 0.9, dashArray: "8 4" }}
        />
      )}
      {descent && (
        <GeoJSON
          key={`${job.id}:descent`}
          data={descent}
          style={{ color: "#d33f2f", weight: 4, opacity: 0.95 }}
        />
      )}
      {points && (
        <GeoJSON
          key={`${job.id}:points`}
          data={points}
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
          <KmlImportLayer key={job.id} job={job} />
        ))}
    </MapContainer>
  );
}
