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
