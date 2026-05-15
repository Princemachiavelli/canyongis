# MAPLIBRE_PLAN

Migration plan from Leaflet 1.9 + react-leaflet to MapLibre GL JS, with
3D terrain rendering as the headline feature unlocked by the switch.

## Goal

- Render the existing PMTiles raster overlays (hillshade, flow, TPI,
  geomorphon, RVT, etc.) and KML/GeoJSON vector overlays on a MapLibre map
  with no loss of current functionality.
- Add an opt-in 3D terrain mode that drapes the same overlays over a
  GPU-rendered DEM mesh, with pan/tilt/rotate controls.
- Reuse the existing tile build pipeline (`gdalwarp` → `gdal_translate -of
  MBTILES` → `pmtiles convert`) — no new server-side renderer.

### Non-goals

- True globe / ellipsoidal math (that's Cesium territory). MapLibre stays
  on Web Mercator.
- Replacing the curated Python preview panels (geomorphon coloring,
  RVT score visualizations). GPU hillshade is added *alongside* them.
- Vector-tile cartography. All overlays stay raster PMTiles.

## Why MapLibre, not Cesium

| Concern | MapLibre | Cesium |
|---|---|---|
| Reads our existing PMTiles | ✅ via `pmtiles` package's `Protocol` | ❌ need XYZ proxy or upload to Ion |
| 3D terrain tile format | terrain-RGB PNG (same gdal pipeline) | quantized-mesh (separate `ctb-tile` build) |
| React integration | `react-map-gl` or thin custom hook | `resium` (awkward, less maintained) |
| GPU hillshade built-in | ✅ `type: hillshade` layer | ❌ shader work |
| Bundle size | ~800 KB | ~3.5 MB |
| AOI drawing | `maplibre-gl-draw` (Mapbox draw, MIT) | hand-rolled with Entities |
| Effort estimate | 1–2 days 2D + 1 day 3D | 1–2 weeks |

The decisive factor is **PMTiles + reusable tile pipeline**: terrain
becomes one new Python function and one new job kind, not a parallel
tiling infrastructure.

## Current architecture inventory

`web/frontend/src/MapView.tsx` is the only file that touches Leaflet
directly. The pieces to port:

| Component | Leaflet primitive | MapLibre equivalent |
|---|---|---|
| `<MapContainer>` | `react-leaflet` | `Map` constructor + container `<div>` |
| OSM + Esri `<TileLayer>` | `L.TileLayer` | `source: { type: 'raster', tiles: [...] }` + `layer: { type: 'raster' }` |
| Hillshade / overlay PMTiles | `pmtiles.leafletRasterLayer` | `source: { type: 'raster', url: 'pmtiles://...' }` |
| ImageOverlay fallback (non-PMTiles preview) | `L.imageOverlay` | `source: { type: 'image', url, coordinates: [[w,n],[e,n],[e,s],[w,s]] }` |
| GeoJSON KML / spines / rappels | `<GeoJSON>` | `source: { type: 'geojson' }` + `line`/`circle`/`symbol` layers |
| AOI rectangle, geoman draw/edit | `@geoman-io/leaflet-geoman-free`, `L.Rectangle` | `@maplibre/maplibre-gl-draw` (rectangle mode) |
| Footprint rectangles | `<Rectangle>` | GeoJSON `line` layer with dashed stroke |
| `multiplyBlend` mix-blend-mode | CSS on layer container | MapLibre raster has no mix-blend; use `raster-opacity` only, or composite in shader fragment via custom layer (post-MVP) |
| `setOpacity` / opacity sliders | per-layer | `map.setPaintProperty(id, 'raster-opacity', v)` / `'line-opacity'` etc. |
| Pan-to-bounds (`PanController`) | `map.fitBounds` | `map.fitBounds([[w,s],[e,n]], { padding })` |

State (`web/frontend/src/state.ts`) is map-library-agnostic — no changes
needed except possibly adding `terrainEnabled: boolean` and
`terrainExaggeration: number`.

## Target architecture

### Frontend

```
web/frontend/src/
  MapView.tsx          # rewritten as MapLibre wrapper
  layers/
    basemaps.ts        # OSM + Esri raster sources/layers
    pmtilesProtocol.ts # one-time addProtocol('pmtiles', ...) registration
    rasterOverlay.ts   # helpers to add/remove a PMTiles raster source+layer
    geojsonOverlay.ts  # helpers for KML, spines, rappels
    terrain.ts         # setTerrain wiring, exaggeration slider
    draw.ts            # maplibre-gl-draw wrapper, rectangle-only mode
  state.ts             # + terrainEnabled, terrainExaggeration, terrainSource
```

`react-map-gl` is **optional** — MapLibre's imperative API is small
enough that a single `useEffect` per layer is clearer than fighting
react-map-gl's declarative model. Decide during Phase 1; lean toward
direct usage.

### Backend (Python)

```
src/canyongis/
  preview_tiles.py
    + write_terrain_rgb_pmtiles(dem_path, out_path, ...) -> TileArchive
    # Encodes DEM elevations into Mapbox Terrain-RGB tiles:
    #   R*256² + G*256 + B - 10000 = meters
    # Pipeline:
    #   1. DEM (any CRS) → reproject to EPSG:3857 via gdalwarp
    #   2. numpy/PIL encode elevation → RGB GeoTIFF (lossless)
    #   3. gdal_translate -of MBTILES -co TILE_FORMAT=PNG
    #   4. gdaladdo (overviews)
    #   5. pmtiles convert
  web/dem_jobs.py
    + DemJobSpec(key="terrain-rgb", ...) so build-dem can emit a
      terrain-RGB PMTiles archive alongside the existing hillshade preview.
      Result fields:
        terrain_rgb_pmtiles_url
        terrain_rgb_tile_min_zoom
        terrain_rgb_tile_max_zoom
        terrain_rgb_tile_bounds_wgs84
```

The frontend reads those fields out of the DEM job's result (already
the pattern in `App.tsx::previewsFor`) and wires up `map.setTerrain` when
the user toggles 3D mode.

## Phased migration

Each phase is independently shippable. The repo can run Leaflet and the
new MapLibre code side-by-side under a feature flag (`?map=maplibre`
query param or env-driven build) during Phase 1 if needed.

### Phase 1 — 2D parity port (1–2 days)

1. **Dependencies.** Add to `web/frontend/package.json` (user-driven Nix
   flake addition for Node side too, since the dev shell builds the
   frontend):
   - `maplibre-gl` (~0.40 stable)
   - `@maplibre/maplibre-gl-draw` (rectangle mode)
   - drop later: `leaflet`, `react-leaflet`, `@geoman-io/leaflet-geoman-free`
   - keep `pmtiles` (it already exports a `Protocol` for MapLibre)
2. **Protocol registration.** New `layers/pmtilesProtocol.ts`:
   ```ts
   import { Protocol } from "pmtiles";
   import maplibregl from "maplibre-gl";
   const protocol = new Protocol();
   maplibregl.addProtocol("pmtiles", protocol.tile);
   ```
   Imported once from `main.tsx`.
3. **Map container.** Rewrite `MapView.tsx` as a single component that
   creates the map on mount (`new maplibregl.Map({ container, style: {...}, center, zoom, maxZoom: 22, hash: false })`) and exposes it via context to children. Style spec inline:
   - OSM raster source + layer
   - Esri imagery raster source + layer (with `paint: { 'raster-opacity': esriOpacity }`)
4. **PMTiles overlays.** A `useEffect` per active hillshade/overlay
   adds/removes a `source` + `layer` pair. Opacity slider → `setPaintProperty(id, 'raster-opacity', v)`.
5. **GeoJSON layers.** Convert each `<GeoJSON>` (descent, approach,
   unclassified, points, profile rappels) into a `geojson` source + a
   `line` or `circle` layer; popups become `Popup`s anchored to click.
   Tooltips/permanent labels become `symbol` layers with `text-field`.
6. **AOI draw.** Initialize `MaplibreDraw` with `rectangle` mode only
   (a Draw-rectangle plugin or constrain via a custom mode). Wire
   `draw.create` / `draw.update` to the existing `setBounds` store
   action. Pan-to-bounds = `map.fitBounds([[w,s],[e,n]])`.
7. **Footprints + KML labels.** Plain GeoJSON line layers; no special
   handling needed beyond style spec entries.
8. **Opacity sliders.** Repoint the existing `OpacitySliders` in
   `App.tsx` to call `setPaintProperty` instead of `setOpacity`. The
   store API stays unchanged.
9. **Mix-blend-mode loss.** MapLibre raster layers don't honor CSS
   `mix-blend-mode` (the canvas is one compositing surface). Decide:
   - Acceptable: drop the multiply blend, rely on `raster-opacity` alone.
   - Preferred for future: add a custom WebGL layer that multiplies the
     overlay sample with the hillshade sample in-shader. Defer.

**Exit criteria for Phase 1:** every current map feature works
identically in 2D (raster overlays, KML lines, AOI draw, pan-to-bounds,
opacity sliders, footprints), with Leaflet removed from the bundle.

### Phase 2 — Terrain-RGB pipeline (0.5 day)

1. **Encoder.** Add `write_terrain_rgb_pmtiles` to
   `src/canyongis/preview_tiles.py`. Reuse `_run`, the gdalwarp →
   gdal_translate MBTILES → pmtiles convert sequence; swap the
   intermediate from a WebP/RGBA image to a 3-band uint8 GeoTIFF whose
   pixel values are the Mapbox Terrain-RGB encoding of the source DEM
   floats. Encoding:
   ```python
   v = np.clip((dem_m + 10000.0) * 10.0, 0, 256**3 - 1).astype(np.uint32)
   r = (v // 65536).astype(np.uint8)
   g = ((v // 256) % 256).astype(np.uint8)
   b = (v % 256).astype(np.uint8)
   ```
   (Factor of 10 = 0.1 m resolution, which is appropriate for 1 m DEMs.)
2. **Job spec.** Register a `DemJobSpec(key="terrain-rgb", ...)` in
   `src/canyongis/web/dem_jobs.py` whose runner calls
   `write_terrain_rgb_pmtiles` and emits the standard `_tile_payload`
   fields via the `terrain_rgb` prefix.
3. **No frontend work yet.** Verify the archive renders correctly in
   the standalone `pmtiles serve` tool or `tilejson-viewer`.

### Phase 3 — 3D mode in the UI (1 day)

1. **Source + setTerrain.** When the user's active DEM job has a
   `terrain_rgb_pmtiles_url`, register it as a `raster-dem` source
   (`{ type: 'raster-dem', url: 'pmtiles://...', tileSize: 256, encoding: 'mapbox' }`)
   and call `map.setTerrain({ source, exaggeration: terrainExaggeration })`.
2. **3D toggle in the map tool dock.** New `mapToolGroup` with a "3D"
   icon (lucide `Mountain` or `Box`). Popover contains:
   - Enable / Disable terrain
   - Exaggeration slider 1.0 – 3.0
   - "Reset view" (pitch=0, bearing=0)
3. **Optional GPU hillshade layer.** Add a `type: 'hillshade'` layer
   bound to the terrain-RGB source, hidden by default. Toggle it from
   the same popover so users can switch between the curated Python
   hillshade panels and the GPU one without going through a job run.
4. **Drape overlays.** All existing raster overlays automatically drape
   on the terrain once `setTerrain` is active — no per-layer config.
   (3D KML lines would need extrusion / `line-emissive-strength`; defer.)

**Exit criteria for Phase 3:** user can toggle a DEM job into 3D,
adjust exaggeration, rotate/tilt the camera, and see hillshade +
existing overlays draped on the terrain mesh.

### Phase 4 — Polish & cleanup (optional)

- Remove the now-redundant Leaflet code paths and the `multiplyBlend`
  CSS class.
- Add a sky layer (`map.setSky({ ... })`) for the 3D mode horizon.
- Replace `react-map-gl` (if adopted in Phase 1) with direct MapLibre
  if the wrapper started feeling in the way.
- Consider exposing the GPU hillshade as a first-class layer in the
  layer-opacity panel, ahead of running the (expensive) hillshade job.

## File-by-file change checklist

| File | Phase | Change |
|---|---|---|
| `web/frontend/package.json` | 1 | add `maplibre-gl`, `@maplibre/maplibre-gl-draw`; drop leaflet stack |
| `web/frontend/src/main.tsx` | 1 | import `./layers/pmtilesProtocol` for side effect |
| `web/frontend/src/MapView.tsx` | 1 | full rewrite — see Phase 1 |
| `web/frontend/src/leafletSetup.ts` | 1 | delete (or repurpose for MapLibre defaults) |
| `web/frontend/src/state.ts` | 3 | add `terrainEnabled`, `terrainExaggeration`, setters |
| `web/frontend/src/App.tsx` | 1, 3 | repoint opacity sliders to `setPaintProperty`; add 3D tool group |
| `web/frontend/src/styles.css` | 1 | drop `.multiplyBlend`, swap `.leaflet-container` selectors for MapLibre canvas |
| `src/canyongis/preview_tiles.py` | 2 | + `write_terrain_rgb_pmtiles` |
| `src/canyongis/web/dem_jobs.py` | 2 | + `DemJobSpec(key="terrain-rgb", ...)` |
| `nix/package.nix` (or wherever Node deps live) | 1 | bump the frontend lock so Nix sees the new packages |

## Risks & open questions

- **AOI drawing UX.** `@maplibre/maplibre-gl-draw` doesn't ship a
  rectangle-only mode by default; we either constrain polygon mode to
  4 corners or use the community `mapbox-gl-draw-rectangle-mode`
  plugin. Either is small; flagged because geoman gave this for free.
- **Mix-blend-mode loss.** Phase 1 will look slightly different than
  today's hillshade-multiplied overlays. Decide up front whether to
  accept that or schedule a custom-layer shader fix.
- **Terrain-RGB precision.** Factor of 10 (`0.1 m` resolution) is fine
  for 1 m USGS DEMs. If we ever ingest sub-decimeter LiDAR, bump to
  factor 100 and check encoding range.
- **Tile size & memory.** 3D mode loads more terrain tiles than 2D
  raster; verify the existing `min_zoom = max_zoom - 4` heuristic in
  `preview_tiles.py` produces enough zoom levels for the camera's full
  range. May need to lower `min_zoom` for terrain archives specifically.
- **Browser / WebGL requirements.** WebGL 1.0 is universal on modern
  hardware; no action needed but worth noting for the project README.
- **PMTiles range requests.** MapLibre + `pmtiles` Protocol does range
  reads over HTTP. The dev server (Vite) handles `Range` headers
  correctly; verify the production FastAPI `StaticFiles` mount does too
  (it does — `starlette.staticfiles` honors Range).

## Effort estimate

- Phase 1 (2D parity): **1–2 days**
- Phase 2 (terrain-RGB encoder + job): **0.5 day**
- Phase 3 (3D UI): **1 day**
- Phase 4 (polish): opportunistic

Total to feature-complete 3D: **~3 days of focused work.**
