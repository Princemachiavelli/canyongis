# Multi-User Support

This project is currently shaped like a local, single-user GIS workbench: one
FastAPI process owns job state, an in-process executor runs work, and generated
artifacts live under one output root. Multi-user support requires turning that
into a project-scoped system with durable metadata, explicit ownership, and
workers that can survive API restarts.

## Required product features

- Authentication: every API request needs an authenticated user identity.
- Authorization: users can only see or mutate projects, jobs, DEMs, KMLs, and
  outputs they own or have been granted access to.
- Projects/workspaces: group DEM containers, KML imports, derived jobs, overlays,
  exports, and generated previews under a project instead of one global job list.
- Roles: start with `owner`, `editor`, and `viewer`.
- Sharing: project membership and optional read-only public/share links.
- Durable job state: queued/running/completed/canceled/failed state must survive
  API process restarts.
- Cooperative cancellation: cancellation must be persisted and observed by worker
  processes, not just in-memory futures.
- Audit trail: record who created, canceled, deleted, imported, and exported
  artifacts.
- Cleanup and retention: quotas, per-project storage usage, artifact deletion,
  and background garbage collection.

## Required refactors

### Replace `jobs.json`

Move job metadata out of `jobs.json` and into a database. PostgreSQL is the
default target for real multi-user use. SQLite can be a transitional local
backend, but the schema should be designed so PostgreSQL/PostGIS is the durable
target.

Minimum job fields:

- `id`
- `project_id`
- `created_by_user_id`
- `kind`
- `status`
- `params`
- `result`
- `error`
- `progress`
- `created_at`
- `updated_at`
- `started_at`
- `finished_at`
- `canceled_at`
- `canceled_by_user_id`

### Add projects and memberships

Suggested tables:

- `users`
- `projects`
- `project_memberships`

Membership fields:

- `project_id`
- `user_id`
- `role`
- `created_at`

API routes should become project-scoped, for example:

- `GET /api/projects`
- `POST /api/projects`
- `GET /api/projects/{project_id}/jobs`
- `POST /api/projects/{project_id}/jobs/build-dem`
- `POST /api/projects/{project_id}/jobs/import-kml-file`

### Model generated assets explicitly

Do not rely only on arbitrary fields inside `job.result` for output discovery.
Keep job result JSON for detailed runner output, but add first-class asset rows.

Suggested `assets` fields:

- `id`
- `project_id`
- `job_id`
- `created_by_user_id`
- `kind` such as `dem`, `kml`, `pmtiles`, `preview_webp`, `gpkg`, `tif`
- `path`
- `url`
- `bounds_wgs84`
- `crs`
- `format`
- `media_type`
- `size_bytes`
- `metadata`
- `created_at`
- `deleted_at`

This makes deletion, export, permissions, and frontend discovery predictable.

### Separate metadata from artifact storage

The database stores ownership, status, bounds, and asset metadata. Large files
stay in artifact storage.

Good storage targets:

- local filesystem for single-machine installs
- S3-compatible object storage for shared or hosted installs

Path layout should be project-scoped:

```text
outputs/
  projects/
    {project_id}/
      jobs/
        {job_id}/
      assets/
        {asset_id}/
```

Centralize all artifact creation/deletion in a storage abstraction so path
safety, URL generation, size accounting, and cleanup are consistent.

### Move jobs out of the API process

The current `ThreadPoolExecutor` is suitable for local use but not multi-user
durability. Long-running DEM, RVT, drainage, PMTiles, KML snap, and profile jobs
should run in worker processes.

Reasonable worker designs:

- PostgreSQL job table with one or more `canyongis-worker` processes polling and
  claiming queued jobs.
- Celery, RQ, or Dramatiq with Redis/PostgreSQL broker.
- A local-only mode can keep the current in-process executor for development,
  but production multi-user mode should use external workers.

Workers should:

- claim jobs atomically
- periodically persist progress
- check for cancellation between major steps
- write outputs through the storage abstraction
- register produced assets
- mark final state in the database

### Add migrations

Use Alembic or an equivalent migration tool. Multi-user work should not depend
on implicit JSON shape changes.

## PostGIS role

PostGIS is useful for multi-user metadata and spatial search. It should not be
the primary store for generated PMTiles, DEM GeoTIFFs, or large preview files.

Use PostGIS for:

- DEM container footprints
- KML bounds and simplified geometries
- queries such as "which KMLs are inside this DEM?"
- spatial search/filtering in a project
- spatial indexes on project-visible assets

Keep large generated artifacts as files or object storage objects.

## PMTiles and raster serving

PMTiles remains the right display artifact for generated previews:

- one portable file per rendered preview
- static serving through `/outputs` or object storage
- Leaflet can render it directly through the PMTiles client
- cleanup is easy because a preview is one archive plus metadata

Do not introduce PostGIS raster just to serve previews. PostGIS raster adds
operational complexity and is not needed for static rendered tiles.

## Recommended target architecture

- FastAPI API server
- PostgreSQL + PostGIS for metadata, permissions, jobs, asset records, and
  spatial indexes
- filesystem or S3-compatible storage for artifacts
- PMTiles/WebP for rendered raster previews
- separate `canyongis-worker` process for long-running jobs
- Alembic migrations
- frontend state scoped by active project

## Migration order

1. Add project IDs and asset records while still using local filesystem storage.
2. Move `jobs.json` into database tables.
3. Add authentication and project membership checks.
4. Move long-running jobs into a worker process.
5. Add PostGIS geometry columns and indexes for bounds and KML/asset extents.
6. Add storage quotas, garbage collection, and audit views.
7. Add sharing/public-link behavior if needed.

## Compatibility notes

Keep local single-user mode available if possible. It is useful for development,
offline use, and field workflows. The implementation can support both modes if
metadata access and artifact storage are abstracted early.
