# Plugin Migration Plan

## Thesis

The project already has the beginning of a plugin model in `src/canyongis/web/dem_jobs.py`: a `DemJobSpec` describes a runnable backend job, its Pydantic request schema, and the visualizations it produces. The rest of the web stack is still strongly coupled to specific concepts and result keys: `build-dem`, `kml-import`, `kml-snap`, `ravine-spine`, `track-profile`, `b_tpi_path`, `kml_url`, `bounds_wgs84`, and ad hoc `*_pmtiles_url` fields.

The migration should preserve the current behavior while moving the web backend, MCP API, and frontend toward a small set of generic plugin contracts:

- Tools submit jobs.
- Jobs consume typed inputs from previous jobs or user-provided parameters.
- Jobs emit typed assets and layers.
- The frontend renders tools, forms, assets, and layers from metadata instead of hardcoded job kinds.

## Goals

- Decouple the core web/backend code from feature-specific implementation details.
- Allow adding new base layer sources without substantial frontend changes.
- Allow adding new analysis features without writing custom FastAPI endpoints and React branches for each feature.
- Unify the DEM job and ravine/spine job callers in `web/frontend/src/api.ts` so jobs are submitted through one generic job API.
- Let plugins declare dependencies on other job outputs. For example, a job needing TPI should default to the latest compatible TPI-producing job, but the UI should expose a dropdown of matching jobs.
- Keep the abstraction minimal. Do not introduce a large framework where a typed registry and a few explicit contracts are enough.

## Current State

### Backend

- `src/canyongis/web/server.py` owns job storage, job execution, auth, project filtering, output URL generation, and most feature-specific HTTP endpoints.
- `JobStore` persists `Job` rows to PostgreSQL and registers assets by scanning result dict fields through `src/canyongis/web/assets.py`.
- `/api/job-specs` and `/api/jobs/{dem_job_id}/run/{job_key}` are already generic, but only for DEM-derived jobs from `dem_jobs.py`.
- `src/canyongis/web/dem_jobs.py` registers hillshade, drainage, geomorphon-ravine, TPI-ravine, curvature, and RVT jobs with `DemJobSpec`.
- `src/canyongis/web/rvt_jobs.py` extends the DEM registry by importing `register()` and adding one more `DemJobSpec`.
- `server.py` still hardcodes non-DEM jobs and layer adaptation: KML import, Ropewiki import, KML snap, track profile, ravine spine, generated KML layer deletion, `_ravine_spine_layers()`, `_compare_raster_path()`, and `_compare_display_high()`.
- `src/canyongis/web/mcp_server.py` duplicates much of `server.py` job submission logic instead of sharing one generic dispatch path.
- `src/canyongis/web/assets.py` infers asset kind from result keys and file extensions, which works but forces plugins to follow implicit naming conventions.

### Frontend

- `web/frontend/src/api.ts` has a generic `runDemJob()` but still has separate clients for `buildDem()`, KML import, `snapKml()`, `runTrackProfile()`, `runRavineSpine()`, and delete-generated-KML-layer.
- `web/frontend/src/App.tsx` groups the UI around hardcoded DEM containers and KML jobs. It checks `job.kind === "build-dem"`, `"kml-import"`, `"kml-snap"`, `"ravine-spine"`, and `"track-profile"`.
- `App.tsx` treats a job as a TPI producer if `result.b_tpi_path` exists. This should become a declared output capability, not a result-key check.
- `App.tsx` synthesizes fake `kml-layer` jobs from ravine-spine result fields. Generated layers should be first-class result metadata, not frontend-generated pseudo-jobs.
- `web/frontend/src/MapView.tsx` has fixed rendering logic for KML-style `descent_geojson`, `approach_geojson`, `unclassified_geojson`, `points_geojson`, raster image overlays, and PMTiles overlays.
- `web/frontend/src/state.ts` stores separate concepts for `activeDemJob`, `activeCompareJob`, `activeHillshade`, `overlay`, `visibleKmls`, and `kmlLabels`. Those should gradually become selected inputs and visible layers keyed by typed layer IDs.

## Current Coupling To Remove

- Job kind checks in `App.tsx`: `build-dem`, `ravine-spine`, `kml-import`, `kml-snap`, and `track-profile`.
- Result-key checks in `App.tsx` and `MapView.tsx`: `bounds_wgs84`, `kml_url`, `b_tpi_path`, `descent_geojson`, `approach_geojson`, `unclassified_geojson`, `points_geojson`, `rappel_candidates_geojson`.
- Backend result-key checks in `server.py`: `b_tpi_path`, `a_geomorphon_path`, `c_curvature_path`, `dem_path`, `kml_path`, generated ravine-spine layer paths, and `out_dir` deletion heuristics.
- Duplicate dispatch code in `mcp_server.py` for `build_dem`, `run_dem_job`, `snap_kml`, `track_profile`, and `ravine_spine`.
- Implicit asset registration by suffix in `assets.py` as the only source of truth for produced files.
- UI assumptions that DEM is the only container/base input and TPI is the only score-raster input.

## Target Architecture

### Plugin Registry

Create a new backend module, probably `src/canyongis/web/plugins.py`, that generalizes `DemJobSpec` into a `ToolSpec` or `JobSpec`.

Minimum spec fields:

- `key`: stable job kind, e.g. `build-dem`, `kml-import-file`, `tpi-ravine`, `ravine-spine`.
- `label`: human-readable tool name.
- `description`: short help text.
- `request_model`: Pydantic model for user parameters.
- `inputs`: tuple of typed input requirements.
- `outputs`: tuple of typed output declarations.
- `layers`: tuple of layer declarations the frontend can render.
- `runner`: callable that receives a normalized execution context and returns a normalized job result.
- `available` and `unavailable_reason`: preserve current optional dependency behavior.
- `ui`: minimal hints for grouping, ordering, default open state, and whether it appears in the map toolbar.

Keep registration simple: a Python module imports `register()` and registers specs at import time, as `rvt_jobs.py` already does.

### Execution Context

Replace ad hoc runner signatures with one context object:

```python
@dataclass(frozen=True)
class JobRunContext:
    output_root: Path
    out_dir: Path
    params: BaseModel
    inputs: dict[str, JobInput]
    progress: Callable[[str], None]
    output_url: Callable[[Path], str]
    project_id: str
    user_id: str | None
```

`JobInput` should be a small typed wrapper around an upstream job and selected output asset/layer. This avoids passing bare `dem_job_id`, `tpi_job_id`, or `compare_job_id` through every endpoint.

### Input Contracts

Plugins should declare what they need instead of encoding dependencies in route URLs.

Useful first input types:

- `aoi_bounds`: user-drawn WGS84 bounds.
- `raster.dem`: a DEM raster asset.
- `raster.score`: a score raster asset, such as TPI, geomorphon, curvature, or future ML probabilities.
- `vector.kml`: imported or generated KML/vector layer.
- `vector.track`: a selectable line feature from a vector layer.
- `job`: fallback input for compatibility where the full upstream job is still needed.

Each input declaration should include:

- `key`: parameter name used in request payloads.
- `kind`: required asset/output kind.
- `required`: boolean.
- `default`: strategy such as `latest-compatible`, `active-container`, or `none`.
- `filters`: optional producer keys, output roles, or project scope.

Examples:

- `ravine-spine` requires `score_raster` with role `ravine-score` and a colocated `dem` input.
- `kml-snap` requires `kml` plus either `dem` or `score_raster` depending on `target`.
- `track-profile` requires `kml_track` plus `dem`.
- DEM-derived preview jobs require one `dem` input.

### Output Contracts

Plugins should return normal JSON plus explicit output metadata. Keep existing result fields during migration, but add structured metadata under `outputs` and `layers`.

Recommended job result shape:

```json
{
  "schema_version": 1,
  "summary": {},
  "outputs": [
    {
      "key": "b_tpi",
      "role": "ravine-score",
      "asset_kind": "raster",
      "path": "/abs/path/b_tpi.tif",
      "url": "/outputs/.../b_tpi.tif",
      "bounds_wgs84": [0, 0, 1, 1],
      "media_type": "image/tiff",
      "metadata": { "display_hi": 1.23 }
    }
  ],
  "layers": [
    {
      "id": "panel_b_tpi",
      "kind": "raster-overlay",
      "label": "TPI ravines",
      "source": { "type": "pmtiles", "url": "/outputs/.../panel_b_tpi.pmtiles" },
      "bounds_wgs84": [0, 0, 1, 1],
      "opacity": 0.72,
      "blend": "multiply"
    }
  ]
}
```

Layer kinds to support first:

- `raster-overlay`: WGS84 image or PMTiles raster overlay.
- `base-raster`: base layer such as hillshade or future DEM-derived basemaps.
- `vector-kml`: KML/vector routes with optional categorized line and point GeoJSON.
- `vector-geojson`: generic GeoJSON layer.
- `point-events`: generated points such as rappel candidates.

### Asset Registry

Keep `assets.py` suffix scanning as a compatibility fallback, but let plugins explicitly emit assets.

Needed changes:

- Add `role`, `label`, `source_job_id`, and `metadata` conventions to emitted output entries.
- Teach `JobStore.register_assets()` to prefer explicit `result.outputs` when present.
- Keep extension inference for old jobs and CLI-generated result dicts.
- Add indexes on `canyongis_assets(project_id, kind)` and potentially JSON metadata fields later only if needed.

### Generic HTTP API

Add generic endpoints while preserving existing endpoints during migration:

- `GET /api/tools`: return all plugin tool specs, including built-ins.
- `GET /api/job-specs`: keep as alias initially, but populate from the unified registry.
- `GET /api/assets`: list assets by project, role, kind, bounds, and producing job.
- `GET /api/layers`: list renderable layers derived from succeeded jobs.
- `POST /api/jobs/{tool_key}`: submit any plugin job with `{ inputs, params }`.
- `POST /api/jobs/{job_id}/outputs/{output_key}/delete`: delete a specific emitted output or layer when supported.

Deprecate later:

- `POST /api/jobs/{dem_job_id}/run/{job_key}`.
- `POST /api/jobs/{tpi_job_id}/ravine-spine`.
- `POST /api/jobs/{kml_job_id}/snap-kml`.
- `POST /api/jobs/{kml_job_id}/track-profile`.
- `DELETE /api/jobs/{job_id}/kml-layer/{layer_key}`.

### Shared Dispatch For FastAPI And MCP

Extract job submission from `server.py` into reusable functions, for example `src/canyongis/web/dispatcher.py`.

Responsibilities:

- Look up a plugin spec.
- Validate request params with `request_model`.
- Resolve declared inputs from IDs or default strategies.
- Create output directory.
- Run the spec runner through `WebContext.submit()`.
- Normalize returned outputs/layers.

Then make FastAPI and MCP call the same dispatcher. `mcp_server.py` should expose a small generic `run_tool(tool_key, inputs_json, params_json)` plus convenience wrappers only where they genuinely improve LLM usability.

### Frontend Model

Update `api.ts` to generic calls:

- `listTools()` / `listJobSpecs()` from unified specs.
- `runTool(toolKey, { inputs, params })`.
- `listAssets()`.
- `listLayers()` or derive layers from normalized job results.

Update app state from feature-specific state to generic selected inputs and visible layers:

- Replace `activeDemJob` with `selectedInputs.dem` or `activeContainer` pointing to an asset/job.
- Replace `activeCompareJob` with selected input by role, e.g. `selectedInputs.scoreRaster`.
- Replace `activeHillshade` and `overlay` with visible layer entries keyed by layer ID and role.
- Keep `visibleKmls` and `kmlLabels` only until vector layers are normalized.

The UI can still present opinionated sections such as Containers, Routes, Terrain Layers, and Ravine Tools, but those sections should be driven by plugin metadata and output roles instead of `job.kind` strings.

### Frontend Rendering

Move hardcoded rendering from `MapView.tsx` into small renderers keyed by layer kind:

- `RasterOverlayLayer` for WGS84 image overlays and PMTiles raster overlays.
- `VectorGeoJsonLayer` for generic GeoJSON layers.
- `RouteLayer` for route/KML layers with categories and labels.
- `PointEventsLayer` for generated points such as rappels.
- `FootprintLayer` for AOI/container bounds.

The map should receive `layers: LayerDescriptor[]` instead of `kmlImports` and `profileJob`. Route selection can be a layer interaction output rather than a hardcoded KML job concept.

## Migration Phases

### Phase 1: Formalize Existing DEM Job Registry

Scope:

- Keep current behavior and endpoints.
- Rename or wrap `DemJobSpec` as the first `ToolSpec` implementation.
- Add `inputs`, `outputs`, and `layers` metadata to current DEM jobs without changing their runners.
- Generate normalized `result.outputs` and `result.layers` in `run_dem_job()` while preserving existing flat keys like `panel_b_tpi_url` and `b_tpi_path`.

Files:

- `src/canyongis/web/dem_jobs.py`
- `src/canyongis/web/rvt_jobs.py`
- `src/canyongis/web/server.py`
- `src/canyongis/web/assets.py`
- `web/frontend/src/api.ts`
- `web/frontend/src/App.tsx`

Acceptance criteria:

- `/api/job-specs` still returns current DEM jobs.
- Existing frontend DEM-derived job buttons still work.
- Each succeeded DEM-derived job includes normalized layers for previews and normalized outputs for produced rasters.
- Asset registration works from normalized outputs and old flat fields.

### Phase 2: Introduce Generic Plugin Dispatch

Scope:

- Add `plugins.py` and `dispatcher.py`.
- Add `POST /api/jobs/{tool_key}`.
- Route the existing DEM job endpoint through the dispatcher.
- Add input resolution for DEM assets/jobs with `latest-compatible` defaulting.

Files:

- `src/canyongis/web/plugins.py`
- `src/canyongis/web/dispatcher.py`
- `src/canyongis/web/server.py`
- `src/canyongis/web/mcp_server.py`
- `tests/web/`

Acceptance criteria:

- `POST /api/jobs/{dem_job_id}/run/{job_key}` and `POST /api/jobs/{job_key}` both work for DEM-derived jobs.
- MCP `run_dem_job()` uses the same backend dispatch path as FastAPI.
- Tests cover request validation, missing input errors, and successful dispatch with a lightweight fake plugin.

### Phase 3: Move Build DEM And KML Import Into Plugins

Scope:

- Register `build-dem` as a plugin that consumes `aoi_bounds` and emits a `raster.dem` output plus a footprint/base layer.
- Register `kml-import-file` and `kml-import-ropewiki` as source plugins that emit `vector.kml` and route layers.
- Keep upload-specific multipart handling thin: it should store the uploaded file or pass bytes to a plugin runner, not own result shaping.

Files:

- `src/canyongis/web/server.py`
- `src/canyongis/aoi_mosaic.py`
- `src/canyongis/validate.py`
- `web/frontend/src/api.ts`
- `web/frontend/src/App.tsx`
- `web/frontend/src/MapView.tsx`

Acceptance criteria:

- DEM containers appear from plugin-emitted `raster.dem` outputs, not `job.kind === "build-dem"`.
- Imported KMLs appear from plugin-emitted `vector-kml` layers, not `job.kind === "kml-import"`.
- Existing build DEM, upload KML, and Ropewiki import flows remain available.

### Phase 4: Move KML Snap, Track Profile, And Ravine Spine Into Plugins

Scope:

- Convert `kml-snap` to a plugin with conditional input requirements based on target mode.
- Convert `track-profile` to a plugin requiring a selected vector track and DEM.
- Convert `ravine-spine` to a plugin requiring a score raster role and DEM.
- Replace `b_tpi_path` detection with a plugin-declared output role such as `ravine-score`.
- Replace generated ravine-spine pseudo KML jobs with emitted vector layers.

Files:

- `src/canyongis/web/server.py`
- `src/canyongis/web/mcp_server.py`
- `src/canyongis/kml_snap.py`
- `src/canyongis/track_profile.py`
- `src/canyongis/ravine_spine.py`
- `web/frontend/src/App.tsx`
- `web/frontend/src/MapView.tsx`
- `web/frontend/src/state.ts`

Acceptance criteria:

- Ravine spine can run from any output with role `ravine-score`, not only `b_tpi_path`.
- Snap can target any compatible raster output with an advertised snap mode or score direction.
- Track profile works from a selected route layer without hardcoding KML result fields throughout the app.
- Deleting generated vector layers uses generic output/layer deletion or job deletion.

### Phase 5: Generic Frontend Forms

Scope:

- Render user parameter forms from `request_schema` with a small schema subset first: boolean, number, string, enum, arrays of numbers/strings, and optional fields.
- Render input selectors from plugin `inputs`: default to active/latest compatible value, but allow choosing another matching job output.
- Remove `window.prompt()` for snap parameters and use the same generic dialog/form path.

Files:

- `web/frontend/src/App.tsx`
- `web/frontend/src/api.ts`
- new `web/frontend/src/ToolDialog.tsx` or similar

Acceptance criteria:

- Existing DEM-derived jobs expose configurable parameters instead of always submitting `{}`.
- Snap radius/sample step are form fields.
- Ravine spine threshold and branch parameters are form fields.
- Adding a new plugin with a simple Pydantic request model produces a usable dialog without editing `App.tsx`.

### Phase 6: Normalize Map Layers And Base Layers

Scope:

- Replace `activeHillshade`, `overlay`, `visibleKmls`, and `profileJob` map props with normalized `LayerDescriptor[]`.
- Support multiple visible overlays if the UI needs it; if not, keep single active overlay but drive it from descriptors.
- Treat OpenStreetMap, Esri, hillshade, DEM previews, and future base sources as base layer descriptors.

Files:

- `web/frontend/src/state.ts`
- `web/frontend/src/MapView.tsx`
- `web/frontend/src/App.tsx`

Acceptance criteria:

- `MapView.tsx` no longer knows about job kinds.
- Route layers, generated ravine-spine layers, profile rappel candidates, raster overlays, and base layers all render from `LayerDescriptor` objects.
- Adding a new raster overlay plugin requires no map-rendering code change if it emits an existing layer kind.

### Phase 7: Cleanup Legacy Endpoints And Contracts

Scope:

- Remove old feature-specific endpoints only after the frontend and MCP have migrated.
- Remove frontend pseudo-job construction for generated KML layers.
- Reduce `server.py` to app setup, auth/project endpoints, job CRUD, and generic tool dispatch.
- Keep CLI entry points unchanged; CLI modularity is separate and should not block the web plugin migration.

Acceptance criteria:

- `server.py` no longer imports feature modules like `ravine_spine`, `track_profile`, `kml_snap`, or `aoi_mosaic` directly except through registered plugins.
- `mcp_server.py` no longer duplicates job execution implementation.
- Tests cover plugin registration, generic API dispatch, asset registration, frontend schema expectations, and backwards compatibility for old job result rows.

## Compatibility Strategy

- Preserve current flat result fields while adding normalized `outputs` and `layers`.
- Keep current endpoints until the frontend and MCP stop using them.
- Keep asset inference by extension as a fallback for old jobs and hand-authored result dicts.
- Do not migrate historical PostgreSQL rows unless a specific query needs it. Normalize them at response time when possible.
- Version normalized result metadata with `schema_version` so future changes are explicit.

## Testing Plan

- Add pure unit tests for plugin registration, duplicate keys, unavailable plugins, request schema serialization, and input resolution.
- Add FastAPI tests using a fake plugin that writes a tiny file under `tmp_path` and emits one layer.
- Add asset tests for explicit `result.outputs` and fallback suffix scanning.
- Add regression tests for old endpoints while they remain supported.
- Add frontend tests if the project already has a test runner; otherwise keep TypeScript type-check/build as the minimum frontend gate.
- Run fast Python tests with `pytest`. Use `nix fmt` before finalizing code changes. Do not run long ML jobs as part of this migration.

## Non-Goals

- Do not design a remote plugin marketplace or dynamic untrusted plugin loading.
- Do not introduce plugin isolation, sandboxing, or separate worker processes yet.
- Do not refactor the ML training/inference CLI into plugins in the first migration. It has different lifecycle and resource requirements.
- Do not require every existing result field to disappear immediately. The migration should be incremental and compatible with existing runs.

## First Implementation Slice

The smallest useful slice is:

- Add `plugins.py` with a generalized spec type compatible with current `DemJobSpec`.
- Have `dem_jobs.py` register through that unified registry.
- Add normalized `outputs` and `layers` for existing DEM-derived jobs.
- Teach `assets.py` to read explicit outputs first.
- Add a generic `POST /api/jobs/{tool_key}` that can run one existing DEM-derived job from a DEM input.
- Leave all current UI flows working.

This slice proves the new contract without forcing the frontend rewrite all at once.
