from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import rasterio

from .io import log

DEFAULT_OUTPUTS = ("forms", "ternary", "positive", "negative", "intensity", "elongation", "azimuth")


def _grass_script() -> str:
    return r"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import grass.script as gs

params = json.loads(Path(sys.argv[1]).read_text())
dem_path = params["dem_path"]
out_dir = Path(params["out_dir"])
search_m = params["search_m"]
skip_m = params["skip_m"]
flat_deg = params["flat_deg"]
dist_m = params["dist_m"]
outputs = params["outputs"]
use_extended = params["extended"]
quiet = bool(params["quiet"])

out_dir.mkdir(parents=True, exist_ok=True)

gs.run_command("r.in.gdal", input=dem_path, output="dem", flags="o", overwrite=True, quiet=quiet)
gs.run_command("g.region", raster="dem", quiet=quiet)

kwargs = {
    "elevation": "dem",
    "search": search_m,
    "skip": skip_m,
    "flat": flat_deg,
    "dist": dist_m,
}
for name in outputs:
    kwargs[name] = name

flags = "m"
if use_extended:
    flags += "e"
gs.run_command("r.geomorphon", flags=flags, overwrite=True, quiet=quiet, **kwargs)

for name in outputs:
    tif = out_dir / f"geomorphon_{name}.tif"
    gs.run_command(
        "r.out.gdal",
        flags="c",
        input=name,
        output=str(tif),
        format="GTiff",
        createopt="COMPRESS=LZW",
        overwrite=True,
        quiet=quiet,
    )
"""


def export_geomorphons(
    dem_path: str | Path,
    out_dir: str | Path,
    *,
    search_m: float = 30.0,
    skip_m: float = 0.0,
    flat_deg: float = 1.0,
    dist_m: float = 0.0,
    outputs: tuple[str, ...] = DEFAULT_OUTPUTS,
    extended: bool = True,
    quiet: bool = True,
    log_callback: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """Run GRASS r.geomorphon against a DEM and export selected outputs as GeoTIFFs.

    log_callback: optional sink for streamed GRASS stderr lines.
    """
    dem_path = Path(dem_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with rasterio.open(dem_path) as src:
        if src.crs is None:
            raise ValueError(
                f"DEM must have a projected CRS for meter-based geomorphons: {dem_path}"
            )
        dem_meta = {
            "width": src.width,
            "height": src.height,
            "crs": str(src.crs),
            "resolution": [float(src.res[0]), float(src.res[1])],
        }

    params = {
        "dem_path": str(dem_path),
        "out_dir": str(out_dir),
        "search_m": search_m,
        "skip_m": skip_m,
        "flat_deg": flat_deg,
        "dist_m": dist_m,
        "outputs": list(outputs),
        "extended": extended,
        "quiet": quiet,
    }

    def emit(line: str) -> None:
        log(line)
        if log_callback is not None:
            log_callback(line)

    with tempfile.TemporaryDirectory(prefix="canyongis-grass-") as tmp:
        tmp_path = Path(tmp)
        script_path = tmp_path / "run_geomorphons.py"
        params_path = tmp_path / "params.json"
        script_path.write_text(_grass_script())
        params_path.write_text(json.dumps(params))
        cmd = [
            "grass",
            "--tmp-project",
            str(dem_path),
            "--exec",
            "python",
            str(script_path),
            str(params_path),
        ]
        emit(f"running GRASS r.geomorphon (search={search_m}m, skip={skip_m}m, flat={flat_deg}deg)")
        env = os.environ.copy()
        if quiet:
            env.setdefault("GRASS_VERBOSE", "0")

        # Stream output line-by-line so callers can show progress while r.geomorphon
        # is running. GRASS modules emit progress to stderr; merge stdout/stderr so
        # we read both from a single pipe.
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        captured: list[str] = []
        try:
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                captured.append(line)
                if log_callback is not None:
                    log_callback(line)
                if not quiet:
                    sys.stdout.write(raw_line)
                    sys.stdout.flush()
        finally:
            returncode = proc.wait()

        if returncode != 0:
            if quiet and log_callback is None:
                for line in captured:
                    sys.stderr.write(line + "\n")
            raise subprocess.CalledProcessError(
                returncode, cmd, output="\n".join(captured), stderr=None
            )

    exported = [str(out_dir / f"geomorphon_{name}.tif") for name in outputs]
    summary: dict[str, object] = {
        "source_dem": str(dem_path),
        "dem": dem_meta,
        "parameters": params,
        "outputs": exported,
        "classes": {
            "1": "flat",
            "2": "peak",
            "3": "ridge",
            "4": "shoulder",
            "5": "spur",
            "6": "slope",
            "7": "hollow",
            "8": "footslope",
            "9": "valley",
            "10": "pit",
        },
    }
    (out_dir / "geomorphons_manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def geomorphons_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-geomorphons",
        description="Export GRASS r.geomorphon landform rasters for a DEM.",
    )
    p.add_argument("dem", type=Path, help="Input projected DEM GeoTIFF.")
    p.add_argument("outdir", type=Path, help="Output directory.")
    p.add_argument("--search-m", type=float, default=30.0, help="Outer search radius in meters.")
    p.add_argument("--skip-m", type=float, default=0.0, help="Inner skip radius in meters.")
    p.add_argument("--flat-deg", type=float, default=1.0, help="Flatness threshold in degrees.")
    p.add_argument("--dist-m", type=float, default=0.0, help="Flatness distance in meters.")
    p.add_argument(
        "--outputs",
        nargs="+",
        default=list(DEFAULT_OUTPUTS),
        choices=[
            "forms",
            "ternary",
            "positive",
            "negative",
            "intensity",
            "exposition",
            "range",
            "variance",
            "elongation",
            "azimuth",
            "extend",
            "width",
        ],
        help="r.geomorphon outputs to export.",
    )
    p.add_argument(
        "--no-extended",
        action="store_true",
        help="Disable r.geomorphon extended form correction.",
    )
    p.add_argument(
        "--grass-verbose",
        action="store_true",
        help="Show GRASS module progress and informational messages.",
    )
    args = p.parse_args(argv)

    summary = export_geomorphons(
        args.dem,
        args.outdir,
        search_m=args.search_m,
        skip_m=args.skip_m,
        flat_deg=args.flat_deg,
        dist_m=args.dist_m,
        outputs=tuple(args.outputs),
        extended=not args.no_extended,
        quiet=not args.grass_verbose,
    )
    log(f"wrote {len(summary['outputs'])} geomorphon rasters to {args.outdir}")
