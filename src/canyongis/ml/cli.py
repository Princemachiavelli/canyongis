from __future__ import annotations

import argparse
import sys
from dataclasses import fields
from pathlib import Path

from .config import MLConfig
from .predict import run_inference
from .train import run_training


def _cli_defaults() -> MLConfig:
    return MLConfig(use_geomorphons=True)


def _add_config_flags(p: argparse.ArgumentParser, defaults: MLConfig, skip: set[str]) -> None:
    """Auto-expose every MLConfig field as --field-name (like config.Config in __main__)."""
    for f in fields(defaults):
        if f.name in skip:
            continue
        val = getattr(defaults, f.name)
        if isinstance(val, tuple):
            item_type = float if any(isinstance(x, float) for x in val) else int
            p.add_argument(
                f"--{f.name.replace('_', '-')}",
                type=lambda s, item_type=item_type: tuple(item_type(x) for x in s.split(",")),
                default=val,
                help=f"(default: {','.join(map(str, val))})",
            )
        elif isinstance(val, bool):
            group = p.add_mutually_exclusive_group()
            group.add_argument(
                f"--{f.name.replace('_', '-')}",
                dest=f.name,
                action="store_true",
                help=f"(default: {val})",
            )
            group.add_argument(
                f"--no-{f.name.replace('_', '-')}",
                dest=f.name,
                action="store_false",
                help=argparse.SUPPRESS if not val else f"(default: {val})",
            )
            p.set_defaults(**{f.name: val})
        else:
            p.add_argument(
                f"--{f.name.replace('_', '-')}",
                type=type(val) if val is not None else str,
                default=val,
                help=f"(default: {val})",
            )


def _cfg_from_args(
    args: argparse.Namespace, defaults: MLConfig, explicit_overrides: set[str] | None = None
) -> MLConfig:
    kwargs = {}
    explicit_overrides = explicit_overrides or set()
    for f in fields(defaults):
        key = f.name
        if hasattr(args, key):
            if key == "in_channels" and key not in explicit_overrides:
                continue
            kwargs[key] = getattr(args, key)
    return MLConfig(**kwargs)


def _explicit_config_overrides(argv: list[str], skip: set[str]) -> set[str]:
    config_keys = {f.name for f in fields(MLConfig()) if f.name not in skip}
    overrides: set[str] = set()
    for arg in argv:
        if not arg.startswith("--"):
            continue
        key = arg[2:].split("=", 1)[0].replace("-", "_")
        if key.startswith("no_"):
            key = key[3:]
        if key in config_keys:
            overrides.add(key)
    return overrides


def train_cli(argv: list[str] | None = None) -> None:
    raw_argv = sys.argv[1:] if argv is None else argv
    defaults = _cli_defaults()
    p = argparse.ArgumentParser(
        prog="canyongis-ml-train",
        description="Train the ML canyon/drainage detector with spatial K-fold CV.",
    )
    p.add_argument("manifest", type=Path, help="JSON manifest: list of {kml_paths, dem_path, ...}")
    p.add_argument("outdir", type=Path, help="Output dir for cache, checkpoints, summary.json")
    _add_config_flags(p, defaults, skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"})
    args = p.parse_args(raw_argv)
    explicit = _explicit_config_overrides(
        raw_argv,
        skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"},
    )
    cfg = _cfg_from_args(args, defaults, explicit)
    cfg.manifest_path = str(args.manifest)
    cfg.out_dir = str(args.outdir)
    run_training(cfg)


def predict_cli(argv: list[str] | None = None) -> None:
    raw_argv = sys.argv[1:] if argv is None else argv
    defaults = _cli_defaults()
    p = argparse.ArgumentParser(
        prog="canyongis-ml-predict",
        description="Run the trained ML detector on a DEM and write prob rasters + vectors.",
    )
    p.add_argument("dem", type=Path, help="Input DEM GeoTIFF (projected CRS).")
    p.add_argument("checkpoint", type=Path, help="Path to best.pt or best_full.pt.")
    p.add_argument("outdir", type=Path, help="Output directory.")
    _add_config_flags(p, defaults, skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"})
    args = p.parse_args(raw_argv)
    explicit = _explicit_config_overrides(
        raw_argv,
        skip={"manifest_path", "out_dir", "dem_path", "checkpoint_path"},
    )
    cfg = _cfg_from_args(args, defaults, explicit)
    cfg.dem_path = str(args.dem)
    cfg.checkpoint_path = str(args.checkpoint)
    cfg.out_dir = str(args.outdir)
    cfg._cli_overrides = explicit  # type: ignore[attr-defined]
    cfg._use_checkpoint_defaults = True  # type: ignore[attr-defined]
    run_inference(cfg)
