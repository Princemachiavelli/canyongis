from __future__ import annotations

import argparse
import gc
from dataclasses import fields
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import transform_bounds

from .config import Config
from .hydrology import compute_flow_accum, compute_hand
from .io import log, read_dem, write_raster, write_vector
from .mapping import render_folium_map
from .masks import make_masks
from .terrain import compute_local_relief, compute_slope, compute_tpi, fill_nans
from .vectors import drainage_polys_gdf, rappel_points_gdf, score_routes


def _log_bounds(name: str, gdf, dem_path: str) -> None:
    """Print layer bounds vs DEM bounds in native + WGS84 CRS. Diagnostic for CRS issues."""
    if gdf.empty:
        return
    with rasterio.open(dem_path) as src:
        dem_native = (src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top)
        dem_wgs = transform_bounds(src.crs, "EPSG:4326", *dem_native)
    lx, ly, ux, uy = gdf.total_bounds
    wgs = gdf.to_crs("EPSG:4326")
    wlx, wly, wux, wuy = wgs.total_bounds
    log(f"  {name} CRS: {gdf.crs}")
    log(f"  {name} native bounds: x=[{lx:.1f},{ux:.1f}] y=[{ly:.1f},{uy:.1f}]")
    log(f"  DEM    native bounds: x=[{dem_native[0]:.1f},{dem_native[2]:.1f}] y=[{dem_native[1]:.1f},{dem_native[3]:.1f}]")
    log(f"  {name} wgs84  bounds: lon=[{wlx:.5f},{wux:.5f}] lat=[{wly:.5f},{wuy:.5f}]")
    log(f"  DEM    wgs84  bounds: lon=[{dem_wgs[0]:.5f},{dem_wgs[2]:.5f}] lat=[{dem_wgs[1]:.5f},{dem_wgs[3]:.5f}]")


def main(cfg: Config) -> None:
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    log("STEP 1: terrain derivatives")
    elev, transform, crs, pixel_size = read_dem(cfg.dem_path)
    filled = fill_nans(elev)
    slope = compute_slope(elev, pixel_size, filled=filled)
    tpi = compute_tpi(elev, pixel_size, cfg.tpi_radius, filled=filled)
    local_relief = compute_local_relief(elev, pixel_size, cfg.local_relief_radius, filled=filled)
    write_raster(out / "slope.tif", slope, transform, crs)
    write_raster(out / "tpi.tif", tpi, transform, crs)
    write_raster(out / "local_relief.tif", local_relief, transform, crs)

    log("STEP 2: flow accumulation & HAND")
    flow_accum = compute_flow_accum(elev, transform)
    stream_mask = flow_accum >= cfg.flow_accum_threshold
    if not stream_mask.any():
        log("WARNING: no streams at threshold; using 99th percentile fallback")
        stream_mask = flow_accum >= np.nanpercentile(flow_accum, 99)
    hand = compute_hand(filled, stream_mask)
    write_raster(out / "hand.tif", hand, transform, crs)
    write_raster(out / "flow_accum.tif", flow_accum, transform, crs)
    del flow_accum, filled
    gc.collect()

    log("STEP 3: cliff & drainage masks")
    channel_mask, _channel_buffer, cliff_faces, rappel_heights = make_masks(
        slope, tpi, hand, local_relief, stream_mask, elev, pixel_size, cfg
    )
    del stream_mask
    write_raster(
        out / "channel_mask.tif", channel_mask.astype(np.uint8), transform, crs, dtype="uint8"
    )
    write_raster(
        out / "cliff_faces.tif", cliff_faces.astype(np.uint8), transform, crs, dtype="uint8"
    )
    write_raster(out / "rappel_heights.tif", rappel_heights, transform, crs)
    del local_relief, channel_mask, _channel_buffer, cliff_faces
    gc.collect()

    log("STEP 4: vectorize rappel points")
    rappel_pts = rappel_points_gdf(rappel_heights, transform, crs)
    if not rappel_pts.empty:
        with rasterio.open(out / "slope.tif") as s:
            rappel_pts["slope_deg"] = [
                float(next(s.sample([(p.x, p.y)]))[0]) for p in rappel_pts.geometry
            ]
        with rasterio.open(out / "tpi.tif") as t:
            rappel_pts["tpi"] = [
                float(next(t.sample([(p.x, p.y)]))[0]) for p in rappel_pts.geometry
            ]
    write_vector(rappel_pts, out / "rappel_points")
    log(f"  rappel points: {len(rappel_pts)}")
    _log_bounds("rappel_pts", rappel_pts, cfg.dem_path)
    del rappel_heights

    log("STEP 5: drainage polygons & route scoring")
    drainage = drainage_polys_gdf(tpi, hand, slope, transform, crs, cfg)
    log(f"  drainage polygons: {len(drainage)}")
    del tpi, hand, slope
    gc.collect()
    scored = score_routes(drainage, rappel_pts, elev, transform, cfg)
    del elev
    gc.collect()
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
        write_vector(routes_out, out / "candidate_routes")
        _log_bounds("candidate_routes", routes_out, cfg.dem_path)

    if cfg.render_folium:
        log("STEP 6: render Folium map")
        with rasterio.open(cfg.dem_path) as src:
            b = src.bounds
            wgs_bounds = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
        render_folium_map(rappel_pts, routes_out, wgs_bounds, out / "canyon_map.html", cfg)

    log("Done.")


def _build_parser() -> argparse.ArgumentParser:
    defaults = Config()
    p = argparse.ArgumentParser(
        prog="canyongis",
        description="Canyoneering route finder via GIS terrain analysis.",
    )
    p.add_argument("dem", type=Path, help="Path to input DEM GeoTIFF (projected CRS, e.g. UTM).")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument("--no-folium", action="store_true", help="Skip Folium HTML map render.")

    # Auto-expose every Config field as --field-name (skip the two positional ones).
    skip = {"dem_path", "out_dir", "render_folium"}
    for f in fields(defaults):
        if f.name in skip:
            continue
        p.add_argument(
            f"--{f.name.replace('_', '-')}",
            type=f.type if isinstance(f.type, type) else type(getattr(defaults, f.name)),
            default=getattr(defaults, f.name),
            help=f"(default: {getattr(defaults, f.name)})",
        )
    return p


def cli(argv: list[str] | None = None) -> None:
    args = _build_parser().parse_args(argv)
    overrides = {k: v for k, v in vars(args).items() if k not in {"dem", "outdir", "no_folium"}}
    cfg = Config(
        dem_path=str(args.dem),
        out_dir=str(args.outdir),
        render_folium=not args.no_folium,
        **overrides,
    )
    main(cfg)


if __name__ == "__main__":
    cli()
