import { useEffect } from "react";
import "./leafletSetup";
import L from "leaflet";
import "@geoman-io/leaflet-geoman-free";
import { ImageOverlay, MapContainer, Rectangle, TileLayer, useMap } from "react-leaflet";
import type { Bounds } from "./api";
import { useAppStore } from "./state";

function leafletBounds(bounds: Bounds): L.LatLngBoundsExpression {
  return [
    [bounds.south, bounds.west],
    [bounds.north, bounds.east]
  ];
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

export function MapView() {
  const bounds = useAppStore((state) => state.bounds);
  const overlay = useAppStore((state) => state.overlay);
  const footprints = useAppStore((state) => state.footprints);

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
      {Object.values(footprints).map((fp) => (
        <Rectangle
          key={fp.id}
          bounds={leafletBounds(fp.bounds)}
          pathOptions={{ color: "#5b6e79", weight: 1, fillOpacity: 0.04, dashArray: "4 4" }}
        />
      ))}
      {bounds && <Rectangle bounds={leafletBounds(bounds)} pathOptions={{ color: "#00bcd4", weight: 2 }} />}
      {overlay && <ImageOverlay url={overlay.url} bounds={leafletBounds(overlay.bounds)} opacity={0.72} />}
    </MapContainer>
  );
}
