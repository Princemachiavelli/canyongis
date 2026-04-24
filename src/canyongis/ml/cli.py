from __future__ import annotations

import argparse
from dataclasses import fields
from pathlib import Path

from .config import MLConfig
from .predict import run_inference
from .train import run_training


def _add_config_flags(p: argparse.ArgumentParser, defaults: MLConfig, skip: set[str]) -> None:
    """Auto-expose every MLConfig field as --field-name (like config.Config in __main__)."""
    for f in fields(defaults):
        if f.name in skip:
            continue
        val = getattr(defaults, f.name)
        if isinstance(val, tuple):
            # decoder_channels etc. Expose as comma-separated ints.
            p.add_argument(
                f"--{f.name.replace('_', '-')}",
                type=lambda s: tuple(int(x) for x in s.split(",")),
                default=val,
                help=f"(default: {','.join(map(str, val))})",
            )
        elif isinstance(val, bool):
            p.add_argument(
                f"--{f.name.replace('_', '-')}",
                action="store_true",
                default=val,
                help=f"(default: {val})",
            )
        else:
            p.add_argument(
                f"--{f.name.replace('_', '-')}",
                type=type(val) if val is not None else str,
                default=val,
                help=f"(default: {val})",
            )


def _cfg_from_args(args: argparse.Namespace, defaults: MLConfig) -> MLConfig:
    kwargs = {}
    for f in fields(defaults):
        key = f.name
        if hasattr(args, key):
            kwargs[key] = getattr(args, key)
    return MLConfig(**kwargs)


def train_cli(argv: list[str] | None = None) -> None:
    defaults = MLConfig()
    p = argparse.ArgumentParser(
        prog="canyongis-ml-train",
        description="Train the ML canyon/drainage detector with spatial K-fold CV.",
    )
    p.add_argument("manifest", type=Path, help="JSON manifest: list of {kml_path, dem_path, ...}")
    p.add_argument("outdir", type=Path, help="Output dir for cache, checkpoints, summary.json")
    _add_config_flags(p, defaults, skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"})
    args = p.parse_args(argv)
    cfg = _cfg_from_args(args, defaults)
    cfg.manifest_path = str(args.manifest)
    cfg.out_dir = str(args.outdir)
    run_training(cfg)


def predict_cli(argv: list[str] | None = None) -> None:
    defaults = MLConfig()
    p = argparse.ArgumentParser(
        prog="canyongis-ml-predict",
        description="Run the trained ML detector on a DEM and write prob rasters + vectors.",
    )
    p.add_argument("dem", type=Path, help="Input DEM GeoTIFF (projected CRS).")
    p.add_argument("checkpoint", type=Path, help="Path to best.pt or best_full.pt.")
    p.add_argument("outdir", type=Path, help="Output directory.")
    _add_config_flags(p, defaults, skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"})
    args = p.parse_args(argv)
    cfg = _cfg_from_args(args, defaults)
    cfg.dem_path = str(args.dem)
    cfg.checkpoint_path = str(args.checkpoint)
    cfg.out_dir = str(args.outdir)
    run_inference(cfg)
