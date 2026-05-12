# canyongis

Canyoneering route finder via GIS terrain analysis.

Given a high-resolution DEM (1–10 m, projected CRS), `canyongis` derives
slope, TPI, local relief, D8 flow accumulation, and HAND, then synthesizes
candidate canyon routes scored by total descent, grade, and cliff count.
Outputs are GeoTIFF rasters, GeoPackage vectors, and an interactive Folium
HTML map.

## Quick start (Nix)

```sh
# enter dev shell
nix develop

# format Nix + Python
nix fmt

# build the package
nix build

# run on a DEM
nix run . -- /path/to/dem_utm.tif /path/to/output

# reproject a geographic-CRS DEM to UTM first
gdalwarp -t_srs EPSG:32612 in.tif utm.tif
```

## CLI

All `Config` fields are exposed as flags. Defaults shown via `--help`:

```sh
canyongis --help
canyongis dem.tif out/ --hand-threshold 25 --no-folium
canyongis ropewiki-sync https://ropewiki.com/Willow_Creek_Canyon --cache-dir runs/train-001/cache
```

`canyongis ropewiki-sync` downloads Ropewiki-attached KML files through the
MediaWiki API, rejects KMLs that do not contain a descent track, reuses an
already-downloaded DEM when one in the manifest/cache fully covers the route,
and otherwise fetches the matching USGS 1-meter DEM tile through TNM Access.

## Ravine Spine Pipeline

The in-progress label-free extractor starts from the multi-scale TPI score
raster produced by `canyongis-ravine-compare` and writes a vector ravine spine
network:

```sh
canyongis ravine-spine /path/to/b_tpi.tif /path/to/dem_utm.tif out/ravine-spine
# or
canyongis-ravine-spine /path/to/b_tpi.tif /path/to/dem_utm.tif out/ravine-spine
```

Current stages:

1. Grey-close the score raster to bridge short score dips along real canyon
   floors.
2. Hysteresis-threshold the score into a ravine mask.
3. Remove tiny connected components.
4. Skeletonize the mask.
5. Extract each component's trunk and major branches.
6. Orient each path downhill and compute elevation-profile attributes:
   `net_drop_m`, `total_drop_m`, `total_gain_m`, `max_step_drop_m`,
   `max_step_gain_m`, `max_step_grade`, `mean_abs_grade`,
   `uphill_fraction`, and `elev_reversals`.
7. Optionally filter suspicious joins with `--max-total-gain-m`,
   `--max-step-gain-m`, and `--max-uphill-fraction`.
8. Snap paths to the local elevation minimum inside the ravine mask.
9. Write `spines.gpkg`/`.kml`, `rappel_candidates.gpkg`/`.kml`,
   `drainage_mask.tif`, `skeleton.tif`, and `summary.json`.

The new profile attributes are meant to expose the two failure modes being
debugged now: spur-crossing joins show up as high cumulative gain, uphill
fraction, or reversal count; short wall detections tend to have high step
grade and little useful length. `rappel_candidates` marks abrupt downhill
windows using `--rappel-window-m`, `--rappel-min-drop-m`, and
`--rappel-min-grade`.

## Layout

```
flake.nix              flake-parts entry point
nix/
  package.nix          canyongis derivation (callPackage)
  pysheds.nix          pysheds (not yet in nixpkgs)
  treefmt.nix          treefmt-nix module: nixfmt + ruff + taplo
src/canyongis/
  config.py            Config dataclass (all tunables)
  io.py                DEM read, raster write, logging
  terrain.py           slope, TPI, local relief
  hydrology.py         flow accumulation (pysheds), HAND
  masks.py             channel/cliff/rappel masks
  vectors.py           rappel points, drainage polys, route scoring
  mapping.py           Folium map render
  __main__.py          CLI entry
tests/                 smoke tests (run via `pytest` or `nix flake check`)
```

## Development

```sh
nix develop
pytest
```

Formatters run via `nix fmt` (treefmt-nix wraps `nixfmt`, `ruff format`,
`ruff check --fix`, `taplo`). CI gate: `nix flake check`.

## Web Interface

The local web interface is built with FastAPI plus a Nix-managed
React/Leaflet frontend:

```sh
nix build .#canyongis-web-frontend
nix build .#canyongis
./result/bin/canyongis web --port 8765
```

Open `http://127.0.0.1:8765`. Draw an AOI on the map, build a DEM mosaic from
TNM products, then run previews such as flow/drainage, TPI/ravine compare, and
ravine spine extraction. Generated rasters, previews, and job metadata are
written under `runs/web/`.
