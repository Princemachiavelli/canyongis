# UI Redesign Plan

Production-grade redesign of the canyongis web UI. Replaces the long, mixed-purpose
left sidebar with a project-scoped, asset-graph-driven workspace.

This plan assumes the plugin registry already exists (`DemJobSpec` with
`InputSpec.kind`, `OutputSpec.role/asset_kind`, `LayerSpec.kind`) — see
`src/canyongis/web/dem_jobs.py`. The redesign **uses** that registry as the
single source of truth for everything the UI shows; no hard-coded job knowledge
in React.

## 1. Goals & non-goals

### Goals
1. **Projects** — distinct, named workspaces. Switching projects swaps the
   visible asset library and job list. Cross-project sharing of expensive
   artifacts (DEMs in particular) without copying files.
2. **Type-driven relationships** — the plugin registry already declares typed
   inputs (`raster.dem`, `raster.score`, `vector.kml`) and typed outputs. The
   UI reads these and never branches on a job key.
3. **Surface dependencies visually** — a user must be able to tell at a glance:
   *"this tool needs a DEM and a score raster; here are the compatible
   candidates in this project."*
4. **Compact, focused layout** — replace the always-scrolling sidebar with an
   asset library, a map, and a contextual inspector. Idle containers stay
   collapsed.
5. **Zero hard-coded UI per job kind**. Adding a new plugin produces a working
   form, input pickers, and layer toggle automatically.

### Non-goals (this phase)
- Multi-tenant org/team model (single-user-per-project is enough for now).
- Drag-and-drop pipeline authoring; the graph view is read-only.
- Mobile / responsive breakpoints below ~1280px.
- Renaming / moving files on disk. Assets are addressed by id, not by path.

## 2. Concepts

### 2.1 Asset
The unit of cross-project sharing and the node type in the dependency graph.
The `canyongis_assets` table already exists (migration `0002_multi_user_tables`,
columns `id, project_id, job_id, kind, path, url, bounds_wgs84, …`). We
**use** it instead of treating job results as the asset model.

- `kind` is the typed taxonomy below (was a freeform string; we tighten it).
- One asset row per discrete artifact a plugin emits (DEM raster, score
  raster, snapped KML, spine KML, …). Plugins already declare these via
  `OutputSpec`; the dispatcher will materialize them.
- An asset is *produced by* exactly one job, but can be *linked into* many
  projects.

### 2.2 Type taxonomy (`AssetKind`)
Hierarchical dotted names. The plugin registry already uses these strings on
`InputSpec.kind` — we'll mirror them on `OutputSpec`.

```
raster.dem                — DEM elevation grid
raster.hillshade          — rendered hillshade (display-only)
raster.score.ravine.tpi   — multi-scale TPI score
raster.score.ravine.geomorphon
raster.score.ravine.curvature
raster.flow_accumulation
vector.kml.route          — original imported route
vector.kml.route.snapped  — route snapped to terrain
vector.kml.spine          — extracted ravine centerlines
vector.geojson.points.rappel_candidates
vector.geojson.profile    — track elevation profile
geom.bounds               — AOI envelope (logical, no file)
```

Compatibility rule: an input declaring `raster.score` matches any asset whose
kind starts with `raster.score.`. The UI uses prefix-match against
`InputSpec.kind` to filter pickers — no special-casing per plugin.

### 2.3 Project
Owns a set of *primary* assets and a set of *linked* assets borrowed from
elsewhere. UI shows them in one library but marks linked rows with a small
chain icon and the originating project name on hover.

A project may contain **one or more AOIs** (`geom.bounds` assets — see §9.4).
The two intended shapes are:

- **Focused project (1 AOI, the dominant case).** Single canyon / single
  region. The AOI is implicit; the UI hides AOI chrome (no picker, no
  grouping) and centers the map on it by default.
- **Aggregate / regional project (N AOIs).** A roll-up view that links in
  AOIs from several focused projects — e.g. *"Zion region"* containing the
  AOIs of *Willow Creek*, *Englestead Hollow*, *Pine Creek*. Typically
  built mostly from `canyongis_project_asset_links` rows pointing at AOI
  assets owned by the focused projects (and, transitively, their derived
  assets the user chose to surface).

The UI's job is to make the focused case feel uncluttered while letting the
aggregate case scale without a separate "regional" mode. See §4 for how the
chrome changes between the two.

### 2.4 Tool (= plugin) and Tool Run (= job)
The terms "tool" and "tool run" replace "job spec" and "job" in user-facing
copy. Internally the spec/job names stay.

## 3. Backend changes

### 3.1 New tables (Alembic `0004_asset_links_and_kinds`)

```sql
-- many-to-many link of assets into additional projects
CREATE TABLE canyongis_project_asset_links (
    project_id text NOT NULL REFERENCES canyongis_projects(id) ON DELETE CASCADE,
    asset_id   text NOT NULL REFERENCES canyongis_assets(id)   ON DELETE CASCADE,
    linked_by_user_id text REFERENCES canyongis_users(id),
    linked_at  text NOT NULL DEFAULT (now() AT TIME ZONE 'UTC')::text,
    PRIMARY KEY (project_id, asset_id)
);
CREATE INDEX canyongis_project_asset_links_asset_id_idx
    ON canyongis_project_asset_links (asset_id);

-- typed dependency edges: which asset(s) a job consumed.
-- Recorded by the dispatcher from the resolved `inputs` dict.
CREATE TABLE canyongis_job_inputs (
    job_id    text NOT NULL REFERENCES canyongis_jobs(id) ON DELETE CASCADE,
    input_key text NOT NULL,            -- the InputSpec.key (e.g. "dem", "score")
    asset_id  text NOT NULL REFERENCES canyongis_assets(id),
    PRIMARY KEY (job_id, input_key, asset_id)
);
CREATE INDEX canyongis_job_inputs_asset_id_idx
    ON canyongis_job_inputs (asset_id);
```

`canyongis_jobs.project_id` stays — it identifies the workspace that ran the
job. Cross-project visibility is by *link*, not by re-tagging the job.

### 3.2 Dispatcher changes (`src/canyongis/web/dispatcher.py`)

Already records `params._inputs`. Extend `submit_tool_job`:

1. **Resolve inputs to asset ids** (not just job ids): when the input value is a
   job id, look up that job's primary asset for the requested kind.
2. **Persist the edges**: after `ctx.submit`, write `canyongis_job_inputs`
   rows from `{input_key: asset_id}` so the dependency graph is queryable
   without parsing JSON.
3. **Materialize outputs as assets**: at job-success time, for each
   `OutputSpec` on the spec, INSERT a `canyongis_assets` row with the right
   `kind` string. Today only paths land in `job.result` — we now also write
   the typed row.

### 3.3 API surface

Drop the result-shape coupling; everything driven by typed assets.

```
GET  /api/projects                              list projects user can see
POST /api/projects                              create
GET  /api/projects/{id}                         details
POST /api/projects/{id}/assets/link             { asset_id }    link existing asset
DELETE /api/projects/{id}/assets/{asset_id}     unlink (does not delete file)

GET  /api/projects/{id}/assets?kind=raster.dem  list visible (primary+linked)
GET  /api/projects/{id}/jobs                    unchanged shape
GET  /api/assets/{id}                           full record incl. provenance
GET  /api/assets/{id}/lineage                   ancestors+descendants (graph)

GET  /api/tool-specs                            existing job-specs endpoint
POST /api/jobs/{tool_key}                       existing generic dispatch
```

The frontend then never inspects `job.result.kind === "build-dem"`; it asks
*"what assets of `raster.dem` does this project see?"* and *"what tools accept
`raster.dem` as input?"*.

## 4. Frontend layout

**Focused project (1 AOI) — default layout:**

```
┌────────────────────────────────────────────────────────────────────────┐
│ Top bar: [Project ▾]  [Search]                  [User ▾] [Settings]    │
├──────────────┬────────────────────────────────────────┬────────────────┤
│              │                                        │                │
│  ASSET       │                                        │  INSPECTOR     │
│  LIBRARY     │              MAP                       │                │
│              │                                        │  (contextual)  │
│  ▸ DEMs (3)  │                                        │                │
│  ▸ Routes    │                                        │  - selected    │
│  ▸ Scores    │                                        │    asset       │
│  ▸ Spines    │                                        │    OR          │
│  ▸ Events    │                                        │  - tool form   │
│              │                                        │                │
│              ├────────────────────────────────────────┤                │
│              │ ACTIVITY  (jobs running / recent)      │                │
└──────────────┴────────────────────────────────────────┴────────────────┘
```

**Aggregate project (N AOIs) — chrome expands:**

```
┌────────────────────────────────────────────────────────────────────────┐
│ Top bar: [Project ▾]  [AOI: All ▾]  [Search]    [User ▾] [Settings]    │
├──────────────┬────────────────────────────────────────┬────────────────┤
│  ASSET       │                                        │                │
│  LIBRARY     │              MAP                       │                │
│              │   (overview: all AOI footprints,       │                │
│  ▾ AOIs (4)  │    click one to focus)                 │  INSPECTOR     │
│   • Willow   │                                        │                │
│   • Englestd │                                        │                │
│   • Pine Ck  │                                        │                │
│   • Mystery  │                                        │                │
│  ▸ DEMs (4)  │                                        │                │
│  ▸ Routes    ├────────────────────────────────────────┤                │
│  ▸ Scores    │ ACTIVITY                               │                │
└──────────────┴────────────────────────────────────────┴────────────────┘
```

### 4.1 Asset Library (left)
- Grouped by `AssetKind` *root* (DEMs / Routes / Scores / Spines / Events).
  Empty groups are hidden — addresses "unused containers shouldn't show".
- Each row: name, small type chip, visibility eye, "open inspector" caret.
- Multi-select with shift; the action bar surfaces compatible tools (see 4.5).
- Search filters across name, type, and provenance.
- Linked-from-other-project assets render with a chain badge.

**AOI handling — single vs. multi:**

- **0 or 1 AOI**: no AOI group in the library, no AOI picker in the top bar,
  no per-AOI filtering. The AOI (if any) is implicit; the map auto-fits to
  its bounds on project switch.
- **2+ AOIs**: an `AOIs` group appears as the *first* library section,
  expanded by default. An `[AOI ▾]` scope picker appears in the top bar with
  a sticky default of `All`. Selecting a specific AOI filters every other
  library section to assets whose lineage descends from that AOI (or
  intersects its bounds, for assets without a clean lineage edge), and pans
  the map to fit it. Selecting `All` shows the regional overview with every
  AOI footprint drawn.

Single-AOI is the *default optimization*. The multi-AOI chrome only renders
when `project.aoi_count > 1`, so a focused project never carries the
aggregate-view overhead.

### 4.2 Map (center)
Unchanged rendering pipeline (`LayerDescriptor` union in `web/frontend/src/layers.ts`).
Map control buttons stay for *map-only* concerns (basemap, opacity, geolocate).
**No more "run X" buttons on the map** — every tool is launched from the
inspector or the asset action bar. The exception is AOI drawing, which is
inherently a map gesture and stays as a map control that opens the
`build-dem` tool form pre-populated with the drawn bounds.

### 4.3 Activity strip (center-bottom)
- Collapsible. Default 1-line height showing running jobs.
- Expand to see recent history with per-job params (the
  `JobParamsDetails` component we already built).
- Click a row → highlights the produced assets in the library and pans the map.

### 4.4 Inspector (right)
Context-sensitive panel with three modes:

1. **Asset mode** — when an asset is selected. Shows: provenance lineage
   (mini-graph), parameters of the job that produced it, list of assets it
   depends on (with click-to-jump), list of tools you can run on it.
2. **Tool mode** — when a tool is being configured. Renders from the
   plugin's Pydantic schema (the existing `ToolDialog.tsx` becomes this panel
   instead of a modal). Inputs auto-filter by `InputSpec.kind` against the
   project's visible assets.
3. **Empty mode** — terse "select an asset or pick a tool" hint and a search.

### 4.5 Tool launching
Two paths, both type-filtered:

- **From an asset** ("what can I do with this DEM?"): the inspector lists
  every tool whose `InputSpec.kind` matches the selected asset(s). Choosing
  one pre-fills that input.
- **From the tool catalogue** ("I want a TPI score"): keyboard `Cmd-K`
  palette listing all tools. Picking one opens the inspector with empty
  inputs and dropdowns scoped to the current project's compatible assets.

## 5. Visualizing dependencies

Two complementary views, both derived from `canyongis_job_inputs`:

### 5.1 Inline lineage (always available)
Inside the asset inspector, an **upward** ancestor view (what produced the
selected asset) and a **downward** descendants view (what was derived from
it). Both are rendered from the same recursive component.

Each edge is labeled with the **`InputSpec.key`** of the consuming job — i.e.
the *slot* the parent filled (`dem`, `score`, `kml`, …). This is what makes
multi-input fan-in legible: the user sees which input each parent satisfied,
not just "these two things were inputs."

**Linear case** (one input per slot):

```
Spines:  spines-94pct          ← selected
 └─ score ←  TPI:  tpi-94pct-2026-05-12
              └─ dem ←  DEM:  willow-creek-1m
```

**Fan-in case** — e.g. a hypothetical ensemble-score tool that consumes
both a TPI and a Geomorphon raster, or `kml-snap` (which takes a KML and a
score raster from independent ancestors):

```
Snapped KML:  willow-snapped-tpi   ← selected
 ├─ kml   ←  Route:  willow-creek-route
 │            └─ (imported, no ancestor)
 └─ score ←  TPI:  tpi-94pct-2026-05-12
              └─ dem ←  DEM:  willow-creek-1m  (†)

(†) shared ancestor — see `DEM: willow-creek-1m` above under kml→… 
```

Rules for the tree renderer:

1. **Group siblings by `input_key`.** Children of a node are bucketed under
   the slot name; if a slot has two parents (e.g. a tool that takes
   `score: list[raster.score]`), they appear under the same heading.
2. **Dedupe shared ancestors.** If the same `asset_id` appears more than
   once in the unfolded tree, only the first occurrence shows its subtree;
   later occurrences render as a single line with a `(†)` marker and a
   click-to-jump link to the canonical occurrence. This keeps a true DAG
   readable in a vertical text layout without lying about it.
3. **Direction switch.** A small toggle flips between *ancestors* and
   *descendants*. Default = ancestors for a leaf asset, descendants for a
   root (e.g. a DEM).
4. **Depth cap.** Default 3 levels; deeper subtrees show a `… (n more)`
   row that expands on click.

API shape (`GET /api/assets/{id}/lineage`):

```jsonc
{
  "root": "spines-94pct",
  "nodes": {
    "spines-94pct":  { "kind": "vector.kml.spine",        "name": "spines-94pct"          },
    "tpi-94pct":     { "kind": "raster.score.ravine.tpi", "name": "tpi-94pct-2026-05-12"  },
    "dem-willow":    { "kind": "raster.dem",              "name": "willow-creek-1m"       }
  },
  "edges": [
    // consumer ← (input_key) ← producer
    { "consumer": "spines-94pct", "input_key": "score", "producer": "tpi-94pct" },
    { "consumer": "tpi-94pct",    "input_key": "dem",   "producer": "dem-willow" }
  ]
}
```

Implementation: recursive React component over the `nodes`/`edges` graph;
dedup tracked by a `Set<asset_id>` carried through the recursion so the same
node is rendered fully once and as a `(†)` reference thereafter.

### 5.2 Project pipeline view (toggleable full-screen overlay)
Read-only DAG of the project's assets and jobs. Idiomatic React choice:
[**React Flow**](https://reactflow.dev) (already heavily used in similar
canvas UIs, MIT-licensed, fits the existing Vite/React stack). Nodes typed
by asset kind, edges = `job_inputs` rows. The view is for orientation, not
authoring; selecting a node focuses it in the library + inspector.

Adding React Flow is a single dep in `flake.nix`; no other lib choice is
forced by it.

## 6. Cross-project asset sharing

The expensive thing is the DEM (downloaded TNM tiles + mosaic). Other assets
are cheap to recompute. So the sharing UX optimizes for DEMs first.

- **Sharing flow**: open an asset's inspector → "Link to project…" → pick
  target project. Calls `POST /api/projects/{id}/assets/link`.
- **Visibility**: linked DEMs appear in the target project's library with a
  chain icon and a tooltip *"linked from <project>"*.
- **Permissions**: a user can only link assets they can already see. Asset
  visibility = `assets.project_id` belongs to a project they're a member of,
  or there is a link row to a project they're a member of.
- **Deletion semantics**: deleting a project unlinks (does not delete) shared
  assets. Deleting the source project of a linked asset is blocked until all
  links are removed.

## 7. State model (frontend)

Replace `web/frontend/src/state.ts`'s ad-hoc map-layer state with:

```ts
type AppState = {
  activeProjectId?: string;
  selection: { kind: "asset"; id: string }
            | { kind: "tool"; key: string; prefill?: Record<string, string> }
            | { kind: "none" };
  // Map rendering stays descriptor-driven; visibility is per asset, not per
  // raw layer key.
  visibleAssetIds: Set<string>;
  baseAssetId?: string;       // chosen hillshade or DEM-as-base
  overlayAssetId?: string;    // optional score overlay
  panRequest?: { bounds: Bounds; nonce: number };
};
```

`LayerDescriptor[]` is still what the map renders — we just compute it from
*visible assets* + their producing job results, instead of from `visibleLayerIds`.
Pseudo-jobs synthesized in `App.tsx` for ravine-spine layers go away: the
spine job emits real assets and they show up directly in the library.

## 8. Migration plan (phased, mergeable)

Each phase ships an end-to-end working UI; no big-bang.

### Phase A — Asset materialization (backend only)
- Add `canyongis_project_asset_links` and `canyongis_job_inputs` tables.
- Extend dispatcher to write `canyongis_assets` rows from `OutputSpec`s and
  edges from resolved inputs.
- Backfill assets from existing succeeded jobs (one-shot script). The current
  UI still works because it reads `job.result.*` paths; assets are additive.
- Add `/api/projects/{id}/assets` and `/api/assets/{id}/lineage`.

### Phase B — Asset Library (frontend, replaces left sidebar)
- New `AssetLibrary` component reading `/api/projects/{id}/assets`, grouped
  by `AssetKind` root. Empty groups hidden.
- Keep current map + activity strip side-by-side with the old sidebar at
  first; toggle behind a `?ui=v2` query param so we can dogfight.
- Reuse `ToolDialog` as a modal for now.

### Phase C — Inspector panel
- Move `ToolDialog` into a right-hand `Inspector` panel; close the modal.
- Add asset-selection mode: lineage, params, "tools I can run".
- Drop the `?ui=v2` flag; v2 becomes default.

### Phase D — Cross-project linking
- `POST /api/projects/{id}/assets/link` endpoint and `Link to project…`
  control in the asset inspector.
- Chain badge in the library.

### Phase E — Pipeline view (optional polish)
- Add React Flow dep.
- Render the project DAG. Read-only.

### Phase F — Cleanup
- Remove the pseudo-job synthesis for ravine-spine layers in `App.tsx`.
- Remove `visibleLayerIds`/`baseLayerId`/`overlayLayerId` in favor of
  `visibleAssetIds`.
- Delete now-unused result-field-name plumbing in the frontend.

## 9. Decisions

1. **Sharing happens at two distinct layers, no content-hash dedup needed.**
   The pipeline has two layers of cacheable data with very different
   characteristics:

   - *Raw TNM products* (downloaded 1m DEM tiles, ~10–100 MB each). These
     are content-addressable by TNM product ID and are already shared
     across every project via the per-server `ctx.dem_cache` directory
     (`canyongis-ropewiki-sync` and `build_aoi_dem_mosaic` both honor it).
     Two AOIs whose footprints overlap will reuse the same cached tiles
     without any project-level wiring. No change needed here.
   - *Built DEM mosaics* (the cropped/merged output of `build-dem`).
     Different AOIs produce different mosaics — different extents, different
     buffers, distinct files. Native content-level dedup would basically
     never hit. **Sharing at this layer is by explicit link** (Section 6):
     a user surfaces an existing built mosaic into another project via
     `POST /api/projects/{id}/assets/link`. No file copy, no hash, just a
     row in `canyongis_project_asset_links`.

   Filesystem-level block dedup (APFS / ZFS / Btrfs) handles the rare
   exact-duplicate case for free; we don't replicate that in the
   application.

2. **Link permissions.** A user can link any asset they can already see
   into any project they own or edit. This doesn't expand visibility of the
   underlying asset — it surfaces it in another workspace the user already
   controls.

3. **Streaming job logs render in the activity strip, not the inspector.**
   The inspector shows the final, completed params via `JobParamsDetails`.
   `progress_cb` lines stream into the per-job row in the activity strip
   (expandable). Rationale: the inspector is selection-driven and tied to
   the *asset*; logs are tied to the *run* and naturally live with the
   running jobs list.

4. **AOI bounds become a first-class asset (`geom.bounds`).** Promote
   `BuildDemToolRequest.bounds` from a transient form value to a saved
   asset. Lets users name AOIs, reuse them for downloading non-DEM data
   for the same region (orthoimagery, land-cover, etc.), and share them
   across projects via the same link mechanism. An AOI asset is logical
   (no file on disk — just `bounds_wgs84` in the asset row) and serves as
   the canonical *region definition* a project can pivot around.

   A project may own / link **multiple** AOI assets. The UI optimizes for
   the 1-AOI case (focused project — see §4) and reveals AOI chrome only
   when `aoi_count > 1` (aggregate / regional project). This means the
   common single-canyon workspace stays uncluttered while regional
   roll-ups — built mostly by linking AOIs (and their derived DEMs /
   scores) from focused projects — scale without a separate UI mode.

## 10. What stays unchanged

- Plugin registry shape (`DemJobSpec`, `InputSpec`, `OutputSpec`, `LayerSpec`).
- `LayerDescriptor` union and the per-kind renderers in `MapView.tsx`.
- The generic dispatch endpoint `POST /api/jobs/{tool_key}`.
- Authentication + project membership tables from migration 0002.
- Pydantic-schema-driven form rendering in `ToolDialog.tsx` (it just moves
  out of a modal into the inspector).

The redesign is **additive to the plugin system, subtractive in the UI** —
the architecture already supports it; the React layer just needs to stop
pretending each job kind is special.
