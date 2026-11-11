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

## One-handed use

The controls are at the bottom and right: the Canyons view has its filter, **Open .gpkg** and
Settings in a toolbar at the bottom; the map's Layers and zoom buttons are at the bottom right
(above the locate button); the Details tabs are at the bottom, above the main bar. The canyon list
and the Ropewiki search results fill from the bottom up, so the first entry is the one nearest the
toolbar and the list starts scrolled to its bottom; the Description tab does the same (its first
section is the lowest, and the tab opens at its bottom). Opening a Description section scrolls it
to the top of the page. While you type in a search field the bottom bar steps aside to leave room
for the results. On the Details → Description tab, tap a
section's heading to collapse or open it, or **double-tap anywhere in a section** to collapse it
(links are left alone).

## Views

A bottom bar switches between four views: **Canyons** (open a file, filter the list), **Map**, **Details** (the selected canyon's tabs: Overview, Forecast, Description, Rappels, Conditions) and **Companion**. Choosing a canyon in the list
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

## Getting any Ropewiki canyon (server)

When the viewer is served by `canyon-viewer-serve` (or `serve.py --build-dir DIR`) and the
network is up, the Canyons view shows **Get a canyon**: search Ropewiki by words in the title,
pick a canyon, and the server prepares a GeoPackage that the page downloads into its own storage
and opens. The section is hidden when the server's API does not answer (offline, or a static
server), and saved files work without it.

- **Data**: canyons live in PostGIS (`GEOPACKAGE_SCHEMA.md`). A canyon is fetched from Ropewiki
  when it is new or its data is over 24 hours old (so asking twice in a day is instant); the
  forecast is refreshed when over 3 hours old. Every download is a fresh export from the
  database, so a file never carries an old forecast.
- **Offline map** (the tick box): imagery, hillshade, topo and contour tiles, built once per
  canyon and reused. It needs US coverage (NAIP, USGS 1 m DEM): other regions get the canyon data
  with the viewer's online map. **The first build of a US canyon can take many minutes and
  downloads gigabytes on the server** (about 2.7 GB of elevation data for Lytle Creek); untick
  it for a quick data-only file, or start the server with `--no-tiles` to never build maps. A
  failed map build still delivers the data and is retried after an hour.
- **Server needs**: PostgreSQL with PostGIS (`CANYONGIS_PG_HOST`/`PORT`/`DB`/`USER`; the dev shell
  starts one on port 5433) and the canyongis package; the flake wrapper provides both interpreter
  and `--build-dir` (default `~/.local/state/canyongis/gpkg-builds`; `--no-build-api` turns the API
  off). If PostgreSQL cannot be reached the server logs "Canyon build API disabled" and serves
  files as before. One build runs at a time; asking for a canyon that is being built rejoins it.
- **Disk**: a canyon's tile file is typically hundreds of MB. Its multi-GB download cache is
  deleted when the build succeeds and kept after a failure so a retry resumes. Exports older than 6
  hours are removed.
- **Safety**: there is no login, so keep it on a private network (the Tailscale setup). The API
  only builds titles that Ropewiki confirms are canyon pages, takes only JSON, and builds are
  limited to one at a time.
- **On the device**: files are kept in IndexedDB, several at once, listed in the canyon list under the search
  (each canyon shows its file's size and date, with Remove, and Open for a file that is not open). A download is checked (SQLite header, full length, free space) before it
  replaces anything, and a write the browser refuses leaves earlier files alone. The single file
  saved by earlier versions of the page is moved into this list.

## Keeping a canyon up to date

Open a canyon, **Details → Overview → Updates**. **Update** asks the server what has changed
since this copy was made (comparing the fingerprints stored in the file with its own), downloads
only the parts that changed (canyon details, tracks and rappels, description, links, condition
reports, ratings) and applies them to the open file; it says "Already up to date" when nothing
differs. **Re-check Ropewiki** does the same but first makes the server fetch the canyon again even
if it did in the last day (it still waits 5 minutes between checks of one canyon). The server
answers from its database, so an update is small, and it works for any file downloaded from the
server or saved earlier: a file that does not record what it holds yet is simply brought fully up
to date the first time.

An update is kept as a small patch on this device and applied again each time the file is opened,
so the saved file itself is not rewritten. **Save GeoPackage** writes the updated data out.
Updating needs the server; the block says so when it cannot be reached. The forecast has its own
refresh on the Forecast tab. See `GEOPACKAGE_SCHEMA.md` for how sections and fingerprints work.

## Links and ratings

On a canyon's **Overview**, below its facts, **Links** (the Ropewiki "Beta sites") and **Ratings** are
collapsed sections; tap one to open it. They show how many there are (and the average rating) so you
can tell without opening them. They stay open or closed while you switch tabs.

## Attribution

Ropewiki's text and data are CC BY-NC-SA. A canyon's Overview ends with an **Attribution** block:
a link to the Ropewiki page, the licence, and the page's authors as Ropewiki lists them. Files
without the author list say so and point to the page history.

## Install as an app (iOS and others)

The page is a PWA: it has a web app manifest, icons and the service worker, so it can be added to
the home screen and runs full screen with no browser bars. It must be served over HTTPS (the
Tailscale setup above) and loaded once online.

- **iPhone / iPad:** open the page in **Safari**, tap Share, then **Add to Home Screen**. iOS has
  no install button, and other iOS browsers cannot do this.
- **Android / desktop Chrome:** use the browser's install option, or the **Install app** button
  that Settings shows when the browser offers it.

**The installed iOS app has its own storage**, separate from Safari: its own saved GeoPackage,
runs and forecasts. Anything recorded in a Safari tab is not in the app. After installing, open
your GeoPackage again inside the app (file picker, or Files). The app also has no `?gpkg=` in its
address, so a file served by `canyon-viewer-serve` has to be opened once from the file picker.
Settings shows whether the browser protects the saved data from clean-up; an installed app is
protected, a Safari tab can lose its data after about a week without a visit.

Not tested on a real iOS device yet: the manifest, icons, share sheet and storage behaviour follow
Apple's documented behaviour but should be tried on one.

## Weather forecast

The **Forecast** tab (Details view) shows the next day hour by hour, then seven days, from
[Open-Meteo](https://open-meteo.com/) (free for non-commercial use, no key). Every day and every
hour has a small weather picture (sunny, partly cloudy, cloudy, rain, thunderstorm, snow, fog).
**Next 24 hours** lists the last 2 hours and the next 24 (continuing into tomorrow, with a day
heading where the date changes): picture, temperature, rain (amount and chance, left out when there
is none) and wind; past hours are dimmed and the current hour is marked. It can be collapsed. Each
day shows weather, high and low, rain amount and chance,
wind and gusts, and daylight; thunderstorms and 10 mm or more of rain are marked. The tab also
says when the forecast was **last updated**.

- A GeoPackage built by `canyongis-ropewiki-gpkg` (or refreshed with `canyongis-gpkg-forecast`)
  already holds a forecast, so the tab works offline from the start.
- **Update forecast**, and opening the tab when the forecast is over 3 hours old, fetch a new one
  when the network is up. Offline, or if the request fails, the last one stays with a note.
  Only the canyon's coordinates are sent, not your position.
- A forecast fetched here is kept in IndexedDB (not in the 100 MB file); the newer of that and the
  file's is shown.
- It is a model value for a grid point, not the canyon floor, and not a flash-flood warning.

**On the Companion run screen**, under the stopwatch, today's forecast also shows as small pictures:
the day's weather, plus a red thermometer when the day's high is above the **hot** limit or a blue
one when it is below the **cold** limit. The limits are in Settings (Temperature pictures), default
85 °F and 60 °F, entered in the chosen units. Each picture has a text label for screen readers (and
a tooltip on desktop). Nothing shows when the forecast is missing or does not cover today.

A forecast saved before hourly values existed shows the days only, with a note to update it.

## Saving runs, GPS fixes and forecasts into a GeoPackage

Settings → **Save GeoPackage** writes everything stored on this device into a copy of the open
file: runs (`runs`, with the GPS track as a line), their events (`run_events`) and GPS fixes
(`run_fixes`), and the newest forecast per canyon. One button does it: on a computer the file is
downloaded as soon as it is built; on a phone or tablet the button then changes to **Save to
Files…** (the share sheet, the way out of an installed iOS app, which needs a fresh tap). The file
is named like `canyons-20261109.gpkg`. Open it in QGIS to see the tracks. The tables are described
in `GEOPACKAGE_SCHEMA.md`.

- Saving again is safe: a run's rows are replaced, and runs from other devices already in the file
  are kept. Runs for canyons that are not in the file are left out (the message says how many).
- The file is not read back for runs: opening it does not import its runs into this device.
- Preparing builds the whole file in memory. With a 100 MB GeoPackage that needs a few hundred MB
  of memory, which a phone may not have; if the tab reloads, save from a smaller file.
- "Export run" still writes the single-run JSON, and now also goes through the share sheet.

## Offline use

The GeoPackage already holds the map layers, but the page itself needs two more things offline,
and has both. A service worker (`sw.js`) caches the page, Leaflet and the GeoPackage library with
its WASM (the CDN files are pinned versions, cached once), and the last-opened GeoPackage is kept
in IndexedDB, so a reload with no network reopens it. Load the page once online over HTTPS (or
localhost) first. `?gpkg=` falls back to the saved copy when the fetch fails. Settings shows
whether the app files are cached and which GeoPackage is saved, with a button to remove it.
When the list of page files changes, bump `VERSION` in `sw.js` (a test checks that every local
file `index.html` loads is in the service worker's list).

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
The Leaflet attribution bar is hidden. The credits of the visible layers (OpenStreetMap, imagery,
elevation data) are behind the small, low-contrast **(i)** next to "Layers": tap it to show them,
tap again to hide them.

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
- `gpkg-update.js`: reads a file's fingerprints, compares them with the server's and applies a delta.
  `updates-ui.js`: the Updates block. `catalog.js`: the server API client. `catalog-ui.js`: the Get a canyon panel. `filecache.js`: the
  files kept on the device.
- `companion-store.js`: IndexedDB storage for runs and forecasts. `companion-ui.js`: the Companion view.
- `gpkg-write.js`: writes runs, events, GPS fixes and forecasts into the open GeoPackage (and
  creates the v3 tables in older files). `forecast.js`: the Open-Meteo request and display
  helpers. `weather-icons.js`: the forecast pictures. `share.js`: share sheet or download. `install.js`: install hints.
- `manifest.webmanifest`, `icons/` (drawn by `icons/make_icons.py`): the installable app.
- `app.js`, `index.html`, `style.css`: the page.
