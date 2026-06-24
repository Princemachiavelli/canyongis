# CanyonGIS GeoLibre plugin

External [GeoLibre](https://github.com/opengeos/GeoLibre) plugin that drives a
canyongis web server: AOI → DEM mosaic builds, Ropewiki KML imports, the
generic job-spec tools (hillshade, ravine scores, KML snap, track profile,
ravine spine, baseline, ML predict), and job activity. Finished job layers are
handed to GeoLibre — vectors through `addGeoJsonLayer`, rasters as native
MapLibre layers mirrored into the store with `registerExternalNativeLayer` —
so GeoLibre's Layers/Style panels own all display and organization.

## Build

```bash
npm install
npm run build   # → dist/plugin.json, dist/canyongis.js, dist/canyongis.css
```

The bundle is a single self-contained ES module (the GeoLibre loader imports
it from a blob URL, so no external imports survive). Zero runtime
dependencies.

## Install into GeoLibre

1. Start the canyongis server (it serves the built plugin):
   `canyongis-web --port 8000` → manifest at
   `http://127.0.0.1:8000/plugins/canyongis/plugin.json`
2. In GeoLibre: **Settings → Manage Plugins → Settings**, add that manifest
   URL (or drop the `dist/` folder into a configured local plugin directory).
3. Activate **CanyonGIS** from the Plugins menu; the control appears on the
   map. Set the server URL, log in (skipped when the server runs with
   `CANYONGIS_SINGLE_USER=1`), pick a project, draw an AOI, build a DEM.

URL parameters (`?canyongis-server=…&canyongis-project=…`) preconfigure the
connection; the server URL/project also persist in the `.geolibre.json`
project via the plugin-state hooks.

## CORS / cookies

The plugin runs on the GeoLibre origin (web: `http://localhost:5173`, desktop:
`tauri://localhost`) and calls the canyongis API cross-origin with
`credentials: "include"`. The server's allowed origins are configurable via
`CANYONGIS_CORS_ORIGINS` (comma-separated; the GeoLibre dev/desktop origins
are the default). Cookie `SameSite` is `lax`, which works for
localhost-to-localhost; set `CANYONGIS_COOKIE_SAMESITE=none` (requires TLS)
when the API and GeoLibre are on different sites.
