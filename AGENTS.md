# canyongis

Geospatial + ML Python package for canyon/route extraction from projected DEM GeoTIFFs, Ropewiki KMLs, and a local FastAPI/React web UI.

## Environment

- Use `nix develop` for almost everything; it supplies GDAL/GRASS/PostGIS/Torch deps, sets `PYTHONPATH=$PWD/src`, and starts a local PostgreSQL cluster in `.pg` on port `5433`.
- Do not `pip install` deps into the repo. Python deps are managed in `flake.nix`/`nix/package.nix`; frontend deps are managed by `web/frontend/package-lock.json` plus `web/frontend/default.nix`.
- If adding new source files that Nix builds/tests must see, `git add` them before `nix build` or `nix flake check`; flakes ignore untracked files.
- This flake uses Python 3.13 in Nix even though `pyproject.toml` supports `>=3.11`.

## Commands

- Format/lint: `nix fmt` runs nixfmt, `ruff format`, `ruff check --fix`, and taplo via `nix/treefmt.nix`.
- Tests: `pytest` defaults to `-m 'not slow'`; run one test with `pytest tests/path/test_file.py::test_name -q`; run slow ML E2E with `pytest -m slow`.
- CI gate: `nix flake check`.
- Package build: `nix build .#canyongis`; it already builds/includes `.#canyongis-web-frontend`.
- Frontend focused check: from `web/frontend`, `npm run build` runs `tsc && vite build`.

## Entry Points

- Main CLI is `src/canyongis/__main__.py` via `canyongis`; it dispatches subcommands such as `train`, `predict`, `ropewiki-sync`, `mosaic-manifest`, `ravine-spine`, and `web`.
- Console scripts are the executable source of truth in `pyproject.toml [project.scripts]`; use those over README examples if they differ.
- Classical DEM/Folium flow lives in `config.py`, `terrain.py`, `hydrology.py`, `masks.py`, `vectors.py`, and `mapping.py`.
- ML training/inference lives in `src/canyongis/ml/`; `MLConfig` fields become CLI flags, and `canyongis-ml-predict` loads missing inference defaults from the checkpoint unless flags override them.
- Web backend is `src/canyongis/web/server.py`; migrations live in `src/canyongis/web/alembic/`; frontend is `web/frontend/src`.

## Web/Postgres

- Web defaults: `canyongis web --port 8765 --output-root runs/web`; it serves `CANYONGIS_FRONTEND_DIST` or `web/frontend/dist` if present.
- PostgreSQL config comes from `CANYONGIS_PG_DSN` or `CANYONGIS_PG_HOST`, `CANYONGIS_PG_PORT`, `CANYONGIS_PG_DB`, `CANYONGIS_PG_USER`, and `CANYONGIS_PG_PASSWORD`; defaults target localhost port `5433`.
- Web tests needing DB use `tests/web/conftest.py::live_postgres`; defaults target TCP `localhost:5433`, while Nix coverage checks override `CANYONGIS_TEST_PG_*` to use `postgresqlTestHook`'s Unix socket.
- `CANYONGIS_SINGLE_USER=1` enables single-user auth paths used by many web tests.
- Changing frontend dependencies requires updating `package-lock.json` and the `npmDepsHash` in `web/frontend/default.nix`.

## ML/GIS Gotchas

- Training/inference can be memory-heavy; avoid launching long ML runs or the webserver unless explicitly asked, and prefer giving the exact command for the user to run.
- Device order is CUDA > MPS > CPU; AMP and gradient scaling are CUDA-only. Default batch sizes are CUDA 6, MPS 4, CPU 2.
- Default ML encoder is `resnet18`; keep changes compatible with 8 GB CUDA VRAM and macOS MPS.
- GRASS `r.geomorphon` support comes from the Nix package wrappers; expect failures outside `nix develop`/Nix-built binaries.
