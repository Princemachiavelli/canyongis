from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from .config import Config
from .io import log, read_dem, write_raster
from .terrain import compute_slope, compute_tpi, compute_local_relief
from .hydrology import compute_flow_accum, compute_hand
from .masks import make_masks
from .vectors import rappel_points_gdf, drainage_polys_gdf, score_routes
from .mapping import render_folium_map


def main(cfg: Config) -> None:
    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)
    out = cfg.out_dir

    log("STEP 1: terrain derivatives")
    elev, transform, crs, pixel_size = read_dem(cfg.dem_path)
    slope = compute_slope(elev, pixel_size)
    tpi = compute_tpi(elev, pixel_size, cfg.tpi_radius)
    local_relief = compute_local_relief(elev, pixel_size, cfg.local_relief_radius)
    write_raster(os.path.join(out, "slope.tif"), slope, transform, crs)
    write_raster(os.path.join(out, "tpi.tif"), tpi, transform, crs)
    write_raster(os.path.join(out, "local_relief.tif"), local_relief, transform, crs)

    log("STEP 2: flow accumulation & HAND")
    flow_accum = compute_flow_accum(elev, transform)
    stream_mask = flow_accum >= cfg.flow_accum_threshold
    if not stream_mask.any():
        log("WARNING: no streams at threshold; using 99th percentile fallback")
        stream_mask = flow_accum >= np.nanpercentile(flow_accum, 99)
    hand = compute_hand(np.nan_to_num(elev, nan=float(np.nanmean(elev))), stream_mask)
    write_raster(os.path.join(out, "hand.tif"), hand, transform, crs)
    write_raster(os.path.join(out, "flow_accum.tif"), flow_accum, transform, crs)

    log("STEP 3: cliff & drainage masks")
    channel_mask, channel_buffer, cliff_faces, rappel_heights = make_masks(
        slope, tpi, hand, local_relief, pixel_size, cfg
    )
    write_raster(os.path.join(out, "channel_mask.tif"), channel_mask.astype(np.uint8), transform, crs, dtype="uint8")
    write_raster(os.path.join(out, "cliff_faces.tif"), cliff_faces.astype(np.uint8), transform, crs, dtype="uint8")
    write_raster(os.path.join(out, "rappel_heights.tif"), rappel_heights, transform, crs)

    log("STEP 4: vectorize rappel points")
    rappel_pts = rappel_points_gdf(rappel_heights, transform, crs)
    if not rappel_pts.empty:
        with rasterio.open(os.path.join(out, "slope.tif")) as s:
            rappel_pts["slope_deg"] = [
                float(next(s.sample([(p.x, p.y)]))[0]) for p in rappel_pts.geometry
            ]
        with rasterio.open(os.path.join(out, "tpi.tif")) as t:
            rappel_pts["tpi"] = [
                float(next(t.sample([(p.x, p.y)]))[0]) for p in rappel_pts.geometry
            ]
    rappel_pts.to_file(os.path.join(out, "rappel_points.gpkg"), driver="GPKG")
    log(f"  rappel points: {len(rappel_pts)}")

    log("STEP 5: drainage polygons & route scoring")
    drainage = drainage_polys_gdf(tpi, hand, slope, transform, crs, cfg)
    log(f"  drainage polygons: {len(drainage)}")
    scored = score_routes(drainage, rappel_pts, elev, transform, cfg)
    if not scored.empty:
        log(f"  max descent:     {scored['total_descent_m'].max():.1f} m")
        log(f"  max grade:       {scored['grade'].max():.3f}")
        log(f"  max cliff count: {scored['cliff_count'].max()}")

    candidates = scored[
        (scored["total_descent_m"] > cfg.min_total_descent)
        & (scored["grade"] > cfg.min_route_grade)
        & (scored["cliff_count"] >= cfg.min_cliff_count)
    ].copy()
    log(f"  candidate routes (after filter): {len(candidates)}")
    routes_out = candidates if not candidates.empty else scored
    if not routes_out.empty:
        routes_out.to_file(os.path.join(out, "candidate_routes.gpkg"), driver="GPKG")

    if cfg.render_folium:
        log("STEP 6: render Folium map")
        with rasterio.open(cfg.dem_path) as src:
            b = src.bounds
            wgs_bounds = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
        render_folium_map(
            rappel_pts, routes_out, wgs_bounds,
            os.path.join(out, "canyon_map.html"), cfg,
        )

    log("Done.")


def cli() -> None:
    cfg = Config()
    if len(sys.argv) >= 2:
        cfg.dem_path = sys.argv[1]
    if len(sys.argv) >= 3:
        cfg.out_dir = sys.argv[2]
    main(cfg)


if __name__ == "__main__":
    cli()
