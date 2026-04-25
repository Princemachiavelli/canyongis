from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import rasterio
from rasterio.warp import transform_bounds

from .ingest import ingest_ropewiki_cli
from .io import log
from .mapping import render_folium_map
from .ml.cli import predict_cli, train_cli
from .ml.config import MLConfig
from .ml.inspect import export_feature_pngs_cli
from .ml.manifest_mosaic import mosaic_manifest_cli
from .ml.predict import run_inference


def _render_map(out_dir: Path, dem_path: str, max_points: int) -> None:
    """Load ML outputs from out_dir and render a Folium HTML map."""
    centerlines_path = out_dir / "canyon_centerlines.gpkg"
    rappels_path = out_dir / "rappel_points.gpkg"
    centerlines = (
        gpd.read_file(centerlines_path) if centerlines_path.exists() else gpd.GeoDataFrame()
    )
    rappels = gpd.read_file(rappels_path) if rappels_path.exists() else gpd.GeoDataFrame()
    with rasterio.open(dem_path) as src:
        b = src.bounds
        wgs_bounds = transform_bounds(src.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top)
    render_folium_map(
        rappels, centerlines, wgs_bounds, str(out_dir / "canyon_map.html"), max_points
    )


def main(cfg: MLConfig, render_folium: bool) -> None:
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log("STEP 1: ML inference")
    run_inference(cfg)

    if render_folium:
        log("STEP 2: render Folium map")
        _render_map(out_dir, cfg.dem_path, cfg.folium_max_points)

    log("Done.")


def cli(argv: list[str] | None = None) -> None:
    """Top-level CLI — dispatch to train, predict, or the default predict+render pipeline."""
    parser = argparse.ArgumentParser(
        prog="canyongis",
        description="Canyon detector powered by an ML U-Net over DEM-derived features.",
    )
    sub = parser.add_subparsers(dest="command")

    # `canyongis train ...` and `canyongis predict ...` delegate to the ml CLI entry points.
    sub.add_parser("train", help="Train the ML model (see canyongis-ml-train --help).")
    sub.add_parser("predict", help="Run inference (see canyongis-ml-predict --help).")
    sub.add_parser(
        "export-features",
        help="Export a cached multiband features.tif to viewable PNGs.",
    )
    sub.add_parser(
        "ropewiki-sync",
        help="Download Ropewiki KMLs and matching 1-meter DEMs.",
    )
    sub.add_parser(
        "mosaic-manifest",
        help="Build one buffered DEM mosaic per KML and update a training manifest.",
    )

    # Default pipeline: positional DEM, checkpoint, outdir + optional --no-folium.
    parser.add_argument("dem", nargs="?", type=Path, help="Path to input DEM GeoTIFF.")
    parser.add_argument("checkpoint", nargs="?", type=Path, help="Path to trained .pt checkpoint.")
    parser.add_argument("outdir", nargs="?", type=Path, help="Output directory.")
    parser.add_argument("--no-folium", action="store_true", help="Skip Folium HTML map render.")
    parser.add_argument(
        "--folium-max-points",
        type=int,
        default=MLConfig().folium_max_points,
        help="Maximum number of rappel points to render on the map.",
    )

    args, extra = parser.parse_known_args(argv)

    if args.command == "train":
        train_cli(extra)
        return
    if args.command == "predict":
        predict_cli(extra)
        return
    if args.command == "export-features":
        export_feature_pngs_cli(extra)
        return
    if args.command == "ropewiki-sync":
        ingest_ropewiki_cli(extra)
        return
    if args.command == "mosaic-manifest":
        mosaic_manifest_cli(extra)
        return

    if not (args.dem and args.checkpoint and args.outdir):
        parser.error(
            "Usage: canyongis DEM CHECKPOINT OUTDIR [--no-folium]\n"
            "   or: canyongis train ...\n"
            "   or: canyongis predict ...\n"
            "   or: canyongis export-features ...\n"
            "   or: canyongis ropewiki-sync ...\n"
            "   or: canyongis mosaic-manifest ..."
        )

    cfg = MLConfig(
        dem_path=str(args.dem),
        checkpoint_path=str(args.checkpoint),
        out_dir=str(args.outdir),
        folium_max_points=args.folium_max_points,
        render_folium=not args.no_folium,
    )
    main(cfg, render_folium=not args.no_folium)


if __name__ == "__main__":
    cli()
