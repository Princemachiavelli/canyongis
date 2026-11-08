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

## Test

```sh
cd web/canyon-viewer
npm install
npm test
```

`test/fixtures/lytle.gpkg` is built by `test/make_fixture.py`, run from the repo root inside
`nix develop`.

## Files

- `gpkg.js`: reads the schema (decodes geometry, loads canyon detail). Works in the browser
  (global `GeoPackage`) and in Node.
- `app.js`, `index.html`, `style.css`: the page.
