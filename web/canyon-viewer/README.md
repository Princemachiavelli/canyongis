# Canyon GeoPackage viewer

A static page (HTML, CSS, JS) that opens a canyon GeoPackage in the browser and shows
canyons on a map, with their description, rappels, condition reports, links and ratings.
Reading uses [@ngageoint/geopackage](https://www.npmjs.com/package/@ngageoint/geopackage)
4.2.10, loaded from jsDelivr. Nothing is uploaded; the file stays in the browser.

## Run

```sh
cd web/canyon-viewer
python3 -m http.server 8000
```

Open http://localhost:8000, then either choose a `.gpkg` file or drop it on the page.
To load a file served next to the page, use `?gpkg=some.gpkg`.

### HTTPS over Tailscale (needed for GPS on a phone)

```sh
nix run .#canyon-viewer-serve -- --port 8443 --gpkg-dir .   # or just `canyon-viewer-serve` in nix develop
```

This reads this machine's Tailscale name, runs `tailscale cert` for it (certificates are kept in
`~/.local/state/canyongis/tailscale-certs` and renewed when due) and serves the viewer on
`https://<name>:8443`. Every `*.gpkg` in `--gpkg-dir` is available at `/gpkg/<name>`, e.g.
`https://<name>:8443/?gpkg=gpkg/canyons.gpkg`. It needs HTTPS enabled for the tailnet, and
`tailscale cert` must be allowed for your user (once: `sudo tailscale set --operator=$USER`).
It serves `web/canyon-viewer` from the current directory when present, else the copy in the
Nix store.

## Views

A bottom bar switches between four views: **Canyons** (open a file, filter the list, map
credits), **Map**, **Details** (the selected canyon's tabs) and **Companion**. Choosing a canyon in the list
zooms the map to it and switches to the Map view.

## Companion mode

The **Companion** view follows your GPS position down the canyon selected in the Canyons view,
a port of the run screen from the canyon-companion app. **Start canyon** begins a run: it shows
the next rappel, how far away it is (along the descent track while you are on it, straight-line
otherwise or when the file has no descent track), and the rappel's height and notes. Back / Next /
All rappels move between rappels, **📍 Save as RN** records where the anchor really
is (it overrides the GeoPackage position from then on, and also locates rappels the file has no
position for), the locate button on the map keeps the map on you (dragging or zooming the map turns it off), and **Finish canyon** ends the run, after which
**Export run** downloads a `canyon-companion-run` v1 JSON file (events plus GPS fixes every 3 s).

While a run is active the page's one map moves into this view, with your position, accuracy
circle, the active rappel and saved positions drawn on it; it moves back to the Map view otherwise.
The start screen lists previous runs with **Export** and **Delete** (also for an unfinished run);
deleting asks first and also removes the rappel positions saved during that run.
Runs, events and fixes are kept in IndexedDB (database `canyongis-companion`), keyed by the canyon's
Ropewiki title and the rappel's sequence number, so they survive reloads and file rebuilds. After
a reload, select the canyon again and choose **Resume run**.

The descent track is the longest `descent` route (else `unknown`); approach and exit routes are
not used. The GeoPackage has no anchors, water hazards or pull notes, so those rows from the
original app are not shown. canyon-companion's server, pipeline and AI extraction are not part
of this page.

**GPS needs a secure context**: geolocation only works on `localhost` or HTTPS, so a phone
reaching `python3 -m http.server` over the LAN gets no position. Serve the page over HTTPS.

## Offline use

The GeoPackage already holds the map layers, but the page itself needs two more things offline,
and has both. A service worker (`sw.js`) caches the page, Leaflet and the GeoPackage library with
its WASM (the CDN files are pinned versions, cached once), and the last-opened GeoPackage is kept
in IndexedDB, so a reload with no network reopens it. Load the page once online over HTTPS (or
localhost) first. `?gpkg=` falls back to the saved copy when the fetch fails. Settings shows
whether the app files are cached and which GeoPackage is saved, with a button to remove it.
When the list of page files changes, bump `VERSION` in `sw.js`.

## Settings

The gear on the Canyons view opens Settings (nothing on the map): **Units** (metric or imperial,
applied to rappel heights, distances and details, and swapping the metre/foot contour overlay),
**Compass** and the offline data above. The compass shows your heading as a wedge on your position
dot during a run, from the device's absolute orientation sensor (iOS asks permission when you
turn it on) or, while moving, the GPS heading. Run exports stay metric.

## Map layers

The **Layers** panel (top right of the map; tap its title to collapse it, collapsed by default
on phones) lists the raster tile layers stored in the
GeoPackage, such as `tiles_osm`, `tiles_imagery` and `tiles_hillshade` from
`canyongis-gpkg-tiles`. Each has a checkbox and an opacity slider; the top row draws on top.
On load only the bottom layer is on, and the hillshade starts at 60% so it can overlay imagery.
Tiles are read straight from the file. Past a layer's last stored zoom the tiles are stretched.
If the file has no tile layers, the online OpenStreetMap map is shown instead.
A GeoPackage built with `canyongis-gpkg-tiles --topo` also has an offline topo map (`tiles_osm`:
relief, forest shading, water, roads and trails, peaks) and contour overlays in metres and feet
(only the one matching the units setting is listed).
The map can only be moved within the area the file covers (the union of its tile layers, or its
canyons plus about 5 km when it has none), and cannot be zoomed out past the level where that area fills the whole map view (no blank space).
The Leaflet attribution bar is hidden; each layer's credit is shown at the bottom of the open
Layers panel (for visible layers) and under "Map credits" in the Canyons view.

## Test

```sh
cd web/canyon-viewer
npm install
npm test
```

`test/fixtures/lytle.gpkg` is built by `test/make_fixture.py`, run from the repo root inside
`nix develop`.

## Files

- `gpkg.js`: reads the schema (decodes geometry, loads canyon detail, lists and reads tile layers). Works in the browser
  (global `GeoPackage`) and in Node.
- `companion.js`: companion logic (track projection, distance to the next rappel, run events,
  export), ported from canyon-companion. Works in the browser and in Node.
- `companion-store.js`: IndexedDB storage for runs. `companion-ui.js`: the Companion view.
- `app.js`, `index.html`, `style.css`: the page.
