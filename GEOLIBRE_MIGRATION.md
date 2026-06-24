# GeoLibre Migration Plan

> **Status (2026-06-11):** Phase 2 MVP implemented. `web/geolibre-plugin/`
> builds the external plugin (`npm run build` → manifest + single ES module +
> CSS, served by the backend at `/plugins/canyongis/plugin.json`). Backend has
> explicit credentialed CORS (`CANYONGIS_CORS_ORIGINS`, GeoLibre origins by
> default), configurable cookie SameSite (`CANYONGIS_COOKIE_SAMESITE`), and a
> `/plugins/canyongis` static mount (`CANYONGIS_GEOLIBRE_PLUGIN_DIST`).
> Per decision, the plugin ships its own AOI→DEM download flow instead of the
> stock `maplibre-gl-national-map` plugin. No database changes were needed —
> the existing jobs/assets schema and generic `/api/job-specs` + `/api/jobs/
> {tool_key}` dispatch serve the plugin as-is.
>
> Nix: `nix build .#canyongis-geolibre-plugin` builds the plugin bundle
> (wired into the canyongis package as `CANYONGIS_GEOLIBRE_PLUGIN_DIST`);
> `nix build .#geolibre-web` builds GeoLibre v1.0.0's static web app with
> CanyonGIS baked in as a bundled plugin (`nix/geolibre-web.nix`). Pointing
> `CANYONGIS_FRONTEND_DIST` at `${geolibre-web}/share/geolibre-web` serves
> GeoLibre same-origin from the canyongis server, sidestepping CORS/cookie
> config entirely. Remaining: COG output alignment, terrain-RGB encoder,
> Google Maps/CalTopo search-bar parity, and retiring `web/frontend/`.

Replace the custom React+Leaflet frontend (`web/frontend/`) with
[GeoLibre](https://github.com/opengeos/GeoLibre) (opengeos, MIT), keeping the
canyongis FastAPI backend as the job/compute engine. Canyongis features become
**GeoLibre external plugins** plus GeoLibre-native data formats.

## What GeoLibre is

- Tauri v2 + React + TypeScript + **MapLibre GL JS** + DuckDB-WASM Spatial + deck.gl.
  Same app runs as desktop (Tauri), browser web app, and a Jupyter anywidget
  (`pip install geolibre`, leafmap-style API).
- npm workspaces monorepo: `@geolibre/core` (Zustand store, `.geolibre.json`
  project format), `@geolibre/map`, `@geolibre/ui`, `@geolibre/plugins`
  (plugin API), `@geolibre/processing` (client-side algorithm registry),
  `backend/geolibre_server` (optional FastAPI sidecar on `127.0.0.1:8765` —
  Whitebox toolbox, Raster→COG / Vector→PMTiles conversions).
- Native data handling: **COG/GeoTIFF, PMTiles, MBTiles, Zarr**, XYZ/WMS/WFS,
  vector tiles, 3D Tiles, LiDAR; vector import via DuckDB-WASM (`KML/KMZ`,
  GPX, GeoJSON, Shapefile, GeoParquet, GeoPackage, FlatGeobuf, …).
- Built-in: layer panel (visibility/opacity/reorder/zoom-to), style panel,
  attribute table, SQL Workspace (DuckDB Spatial), terrain + globe controls,
  Planetary Computer / Earth Engine / Overture / federal Web Services data
  integrations, project save/share/embed (`viewer.geolibre.app/?url=…`).

## Plugin API (the integration surface)

```ts
interface GeoLibrePlugin {
  id; name; version; activeByDefault?;
  urlParameterNames?: string[];
  activate(app: GeoLibreAppAPI); deactivate(app);
  handleUrlParameters?(app, params: URLSearchParams);
  getProjectState?(): unknown;                  // persisted into .geolibre.json
  applyProjectState?(app, state);
  get/setMapControlPosition?(…);
}
interface GeoLibreAppAPI {
  setBasemap; addGeoJsonLayer(name, fc, sourcePath?);
  getActiveBasemap; onBasemapChange;
  fetchArrayBuffer?; fitBounds?(bbox);
  getMap?(): maplibregl.Map | null;             // raw map escape hatch
  // NOTE: docs/plugin-api.md lags the source; types.ts additionally has:
  registerExternalNativeLayer?(reg: {           // raw MapLibre layers, but
    id; name; type?; source?; geojson?;         // store-integrated: layer
    nativeLayerIds; sourceIds?; beforeId?;      // panel, opacity, persistence
    opacity?; style?; metadata?; sourcePath?;
  });
  unregisterExternalNativeLayer?(id);
  getDeckGL?(): GeoLibreDeckGL;                 // host's deck.gl modules
                                                // (COGLayer-style rendering)
  exportTextFile?; importTextFile?;             // host save/open dialogs
  addMapControl(IControl, position); removeMapControl;
  setBuiltInMapControlVisible/Position(…);
}
```

External plugins are ES modules loaded from a manifest URL
(`import(URL.createObjectURL(...))`); registry at plugins.geolibre.app. A URL
parameter only activates an already-registered plugin.

## Feature mapping: current frontend → GeoLibre

| Current (web/frontend) | GeoLibre equivalent |
|---|---|
| `MapView.tsx` (Leaflet, react-leaflet, geoman, pmtiles glue) | Delete. GeoLibre map workspace; PMTiles + COG native. Subsumes all of `MAPLIBRE_PLAN.md` Phase 1. |
| `OpacitySliders`, layer toggles | Built-in layer panel + style panel. Delete. |
| Hillshade/TPI/RVT/geomorphon PMTiles overlays | Add as PMTiles or COG layers served by canyongis backend. Consider emitting **COG directly** (rio-cogeo) and skipping the gdal→MBTiles→pmtiles chain for single-AOI rasters. |
| 3D terrain (`MAPLIBRE_PLAN` Phases 2–3) | Built-in terrain control. For *our* 1 m DEM terrain: plugin calls `getMap().addSource({type:'raster-dem',…}) + setTerrain()` with our terrain-RGB PMTiles (Phase 2 encoder still needed). |
| KML upload | Native drag-and-drop KML/KMZ/GPX (DuckDB-WASM). Backend upload still needed when the KML must feed a server job (snap, profile, training). |
| Search bar (`classifySearch`: geocode, Ropewiki, CalTopo, KML URL, Google Maps) | **canyongis-import plugin**: a map control with the same search box + `handleUrlParameters` (`?ropewiki=…`, `?kml=…`) so imports are shareable links. Port `classifySearch` logic verbatim. |
| AOI draw (geoman rectangle) | Plugin map control using MapLibre draw (GeoLibre has no public draw API yet) or a minimal box-drag handler on `getMap()`. |
| `ToolDialog` / Inspector tool launch, `JobActivityPanel` | **canyongis-jobs plugin**: panel (rendered inside an `IControl`) that reads `/api/job-specs`, renders generic forms, submits via the generic dispatch from `PLUGIN_MIGRATE.md`, polls `/api/jobs`, and adds result layers on success. |
| `AssetLibrary`, `PipelineView`, lineage | Dropped (decided): GeoLibre's layer panel owns display/organization; backend still records lineage. |
| `ProjectPicker`, `UserMenu`, `AuthGate` | No GeoLibre concept of users. Plugin holds the auth token (login form in plugin panel; bearer token to canyongis API). canyongis project id stored via `getProjectState` → travels inside `.geolibre.json`. |
| `state.ts`, `api.ts`, `layers.ts` | `api.ts` largely survives as the plugin's client; the rest deleted. |

## Workflow re-implementation as plugins

Two external plugins, built in a new `web/geolibre-plugins/` npm package
(outputs one ES module + manifest each, served by the canyongis backend):

1. **`canyongis-import`** — search box control; Ropewiki/CalTopo/Google
   Maps/KML-URL/coordinate classification; calls backend import endpoints;
   adds resulting tracks via `addGeoJsonLayer` + `fitBounds`.
2. **`canyongis-jobs`** — AOI draw, DEM mosaic build, job-spec-driven tool
   forms (hillshade, drainage, TPI/geomorphon ravine, curvature, RVT,
   baseline, KML snap, track profile, ravine spine, ML predict), job
   activity list with cancel/retry, result-layer registration
   (GeoJSON via `addGeoJsonLayer`; rasters via `registerExternalNativeLayer`
   so they appear in the layer panel and persist), 3D terrain toggle for the
   active DEM.

The backend work in `PLUGIN_MIGRATE.md` (generic plugin dispatch, typed
inputs/outputs, asset registry) is a **prerequisite, not obsolete** — it
becomes the wire contract the GeoLibre plugins consume. `UI_DESIGN.md`'s
frontend phases (Asset Library, Inspector, Pipeline view) are superseded.

## Phases

### Phase 0 — Validation spike (½ day, no code)
Run GeoLibre web build locally; manually drag in existing run outputs:
a hillshade PMTiles URL, a preview GeoTIFF (convert one to COG), a Ropewiki
KML. Confirm rendering, opacity, attribute table, terrain. Kills the plan
early if rendering quality is unacceptable.

### Phase 1 — Backend output alignment (1–2 days)
- Add COG output (rio-cogeo) alongside/instead of PMTiles for raster job
  results; keep PMTiles for large mosaics.
- Terrain-RGB PMTiles encoder (`MAPLIBRE_PLAN` Phase 2, unchanged).
- CORS: allow the GeoLibre origin (localhost:5173 dev / wherever hosted).
- Finish `PLUGIN_MIGRATE.md` Phases 1–3 (generic dispatch + explicit outputs)
  so plugins never need feature-specific endpoints.

### Phase 2 — `canyongis-jobs` plugin MVP (2–4 days)
AOI draw → DEM mosaic → run hillshade → layer appears. Then generic tool
forms from `/api/job-specs`, polling, cancel/retry, terrain toggle.

### Phase 3 — `canyongis-import` plugin (1 day)
Port `classifySearch`, search control, URL parameters.

### Phase 4 — Auth + project round-trip (1–2 days)
Token login in plugin panel; `getProjectState`/`applyProjectState` carrying
canyongis project id + layer↔asset map so reopening a `.geolibre.json`
restores server layers.

### Phase 5 — Decommission (½ day)
Delete `web/frontend/`; update `flake.nix` (`canyongis-web-frontend` →
plugin bundle build); serve GeoLibre web build (or document desktop app +
plugin manifest URL).

## Opportunities

- **Deletes ~all bespoke map UI** (MapView/OpacitySliders/layer plumbing) and
  the entire MapLibre migration plan's Phase 1/3 work — terrain, globe,
  layer panel, styling, attribute table come free.
- **Jupyter widget**: `geolibre.Map().add_cog("features.tif")` gives an
  interactive inspector for ML feature stacks and predictions in notebooks —
  better than `canyongis-ml-export-features` PNGs.
- **DEM acquisition**: Planetary Computer integration exposes USGS 3DEP; the
  federal Web Services plugin may cover TNM. Could complement or replace
  parts of `ingest.py` for interactive exploration (training ingest stays).
- **SQL Workspace**: ad-hoc DuckDB Spatial queries over rappel points /
  centerlines / Ropewiki tracks without writing Python.
- **Whitebox sidecar**: interactive flow accumulation / hydrology overlaps
  our `hydrology.py` — useful for QA comparisons, not a pipeline replacement.
- **Sharing**: results as a `.geolibre.json` URL on viewer.geolibre.app —
  cheap public demo of detections.
- **Desktop app** (Tauri) at zero cost.

## Limitations / risks

- **Thin plugin UI surface** (accepted): plugins get map controls
  (`IControl`) and the raw map — no sanctioned panels/dialogs/menus.
  **Decision: let GeoLibre own data display/organization** (its layer panel,
  style panel, attribute table); the jobs plugin keeps only a compact
  launcher/activity control. Drop AssetLibrary/Inspector/PipelineView — the
  backend keeps recording lineage; no custom layer-organization UI.
  Built-in plugins (National Map, Whitebox, Components) all live as
  `IControl` panels (~340px wide), so this is the intended idiom.
- ~~`addGeoJsonLayer` is the only typed layer-add API~~ **Resolved — docs
  lag the source.** `types.ts` has `registerExternalNativeLayer()`: a plugin
  adds raw MapLibre sources/layers (or deck.gl via `getDeckGL()`) and
  registers them in the store with opacity/style/metadata, so they appear in
  the layer panel and persist in `.geolibre.json`. The store also has a
  first-class `type: "cog"` layer (deck.gl COGLayer); `raster-layer-sync.ts`
  shows the control↔store sync pattern to copy.
- **No multi-user/auth concept — and none planned.** The roadmap has no
  unchecked items except the plugin marketplace; local-first single-user is
  the design, with sharing via static `.geolibre.json` URLs. All account UX
  (login, project membership) lives in our plugin; `MULIT_USER_FEATURE.md`
  server-side work survives unchanged.
- **No job/lineage UI primitives** — though the Whitebox toolbox proves the
  pattern: its sidecar runs tools as async jobs (`JobState` dict + polling)
  with progress shown in its own control panel. Our jobs plugin does the
  same against the canyongis API.
- ~~No public draw API~~ **Mostly resolved**: the built-in
  `maplibre-gl-geo-editor` plugin provides drawing controls; AOI rectangle
  can come from GeoEditor geometry or a 20-line drag-box handler on
  `getMap()`.
- **Processing menu is not plugin-extensible**: `@geolibre/processing` is a
  hardcoded `ALGORITHMS` array + a sidecar client pinned to `127.0.0.1:8765`.
  canyongis tools cannot appear under Processing without forking; they live
  in our plugin control instead. (Upstream PR opportunity: a
  `registerAlgorithm()` hook.)
- **v1.0 churn**: young project (~180 commits); plugin API may break — and
  demonstrably moves faster than its docs. Pin a release; the
  external-plugin model keeps us decoupled from their build.
- **Mix-blend multiply** (current hillshade×overlay compositing) still
  unsupported on MapLibre raster layers — same loss as `MAPLIBRE_PLAN`;
  accept opacity-only or pre-composite server-side.

## GeoLibre sidecar vs. canyongis server

**Keep the canyongis FastAPI server; do not adopt the sidecar structure.**
`backend/geolibre_server` is a localhost-only helper, not a framework:

- Its generic `POST /run` is a **501 placeholder**; real functionality is
  per-domain routers (`/whitebox`, `/conversion`, `/raster`, `/vector`).
- Jobs are an in-memory dict (`_JOBS`, max 100, `threading`) — no
  persistence, no auth, no users; CORS pinned to `localhost:5173` + Tauri
  origins; even has a `POST /shutdown`. canyongis (Postgres JobStore,
  auth, assets, projects, MCP) is strictly more capable.
- There is no registration mechanism for third-party sidecars or extra
  Processing-menu endpoints — the frontend talks to its own sidecar only.

Worth borrowing, not adopting: the Whitebox `JobState`/polling JSON shape as
a sanity check on our generic dispatch contract, and `GEOLIBRE_CONVERSION_ROOTS`
-style path confinement if the canyongis server ever serves untrusted users.

Useful adjacent find: the built-in **`maplibre-gl-national-map`** plugin is a
USGS National Map browser — TNM imagery/services in the UI for free
(display only; `ingest.py` still owns DEM download for training).

## Decision points

1. **Embed vs. external plugin**: fork/vendor GeoLibre and register plugins
   internally (full UI access, heavy maintenance) vs. external plugin
   manifest (decoupled, thin API). **Recommend external**, contributing API
   gaps (Processing-menu registration) upstream.
2. **PMTiles vs. COG** for raster outputs: COG is simpler (one file, GeoLibre
   native via deck.gl COGLayer, Jupyter-friendly); PMTiles better for huge
   mosaics. Recommend COG default, PMTiles retained for mosaics.
3. ~~Keep or drop lineage UI~~ **Decided: drop.** GeoLibre owns layer
   display/organization; backend keeps recording lineage.
