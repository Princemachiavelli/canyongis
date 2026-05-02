from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.merge import merge
from rasterio.warp import transform_bounds
from shapely.geometry import LineString, MultiLineString, box

from ..ingest import _download_file, _extract_bounds, _item_download_url, query_tnm_products
from ..io import log
from ..validate import parse_kml


def _safe_stem(path: str | Path) -> str:
    stem = unquote(Path(path).stem)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    return stem or "descent"


def _canonical_kml_path(kml_path: str, kml_dir: Path) -> str:
    path = Path(kml_path).expanduser()
    wanted = unquote(path.name)
    for candidate in kml_dir.glob("*.kml"):
        if unquote(candidate.name) == wanted:
            return str(candidate)
    if path.exists():
        return str(path)
    return kml_path


def _manifest_kml_paths(row: dict[str, object], kml_dir: Path) -> list[str]:
    raw_paths: list[object] = []
    if isinstance(row.get("kml_path"), str):
        raw_paths.append(row["kml_path"])
    if isinstance(row.get("kml_paths"), list):
        raw_paths.extend(row["kml_paths"])

    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            continue
        paths.append(_canonical_kml_path(raw, kml_dir))
    return paths


def _dedupe_manifest_kml_paths(rows: list[dict[str, object]], kml_dir: Path) -> list[str]:
    by_kml: dict[str, str] = {}
    for row in rows:
        for canonical in _manifest_kml_paths(row, kml_dir):
            path = Path(canonical).expanduser()
            key = str(path.resolve()) if path.exists() else canonical
            if key not in by_kml:
                by_kml[key] = canonical
    return list(by_kml.values())


def _sample_valid_fraction(path: Path) -> float:
    with rasterio.open(path) as src:
        scale = max(src.width / 512, src.height / 512, 1)
        out_shape = (max(1, math.ceil(src.height / scale)), max(1, math.ceil(src.width / scale)))
        arr = src.read(1, out_shape=out_shape, masked=True)
    return float(np.ma.count(arr) / arr.size)


def _source_dem_paths(dem_dir: Path, output_dem_dir: Path, min_valid_fraction: float) -> list[Path]:
    paths: list[Path] = []
    output_dem_dir = output_dem_dir.resolve()
    for path in sorted(dem_dir.rglob("*.tif")):
        try:
            path.resolve().relative_to(output_dem_dir)
            continue
        except ValueError:
            pass
        if path.name.endswith("_mosaic.tif") or "_mosaic_" in path.name:
            continue
        valid_fraction = _sample_valid_fraction(path)
        if valid_fraction < min_valid_fraction:
            log(
                f"skipping mostly-empty source DEM: {path} "
                f"(sample valid fraction {valid_fraction:.3f})"
            )
            continue
        paths.append(path)
    return paths


def _safe_output_nodata(nodata: float | int | None, dtype: str) -> float | int:
    kind = np.dtype(dtype).kind
    if nodata is None:
        return -9999.0 if kind == "f" else 0
    if kind == "f" and (not np.isfinite(nodata) or abs(float(nodata)) > 1.0e20):
        return -9999.0
    return nodata


def _descent_bounds_in_crs(kml_path: str | Path, crs, buffer_m: float) -> tuple[float, float, float, float]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError(f"KML has no descent lines: {kml_path}")
    descent = features.descent_lines.to_crs(crs)
    minx, miny, maxx, maxy = descent.total_bounds
    return (
        float(minx - buffer_m),
        float(miny - buffer_m),
        float(maxx + buffer_m),
        float(maxy + buffer_m),
    )


def _descent_bounds_wgs84(kml_path: str | Path) -> tuple[float, float, float, float]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError(f"KML has no descent lines: {kml_path}")
    return tuple(float(v) for v in features.descent_lines.total_bounds)


def _intersects_bounds(path: Path, bounds: tuple[float, float, float, float]) -> bool:
    with rasterio.open(path) as src:
        return box(*bounds).intersects(box(*src.bounds))


def _bounds_intersect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _bounds_union(bounds: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    if not bounds:
        raise ValueError("bounds must not be empty")
    return (
        min(b[0] for b in bounds),
        min(b[1] for b in bounds),
        max(b[2] for b in bounds),
        max(b[3] for b in bounds),
    )


def _cluster_kml_paths(kml_paths: list[str], buffer_m: float) -> list[list[str]]:
    """Group KMLs whose buffered descent bounds overlap, including transitive chains."""
    inflated = {
        kml_path: _inflate_wgs84_bounds(_descent_bounds_wgs84(kml_path), buffer_m)
        for kml_path in kml_paths
    }
    remaining = set(kml_paths)
    groups: list[list[str]] = []
    while remaining:
        start = min(remaining)
        remaining.remove(start)
        group = [start]
        queue = [start]
        while queue:
            current = queue.pop()
            current_bounds = inflated[current]
            for candidate in sorted(tuple(remaining)):
                if _bounds_intersect(current_bounds, inflated[candidate]):
                    remaining.remove(candidate)
                    group.append(candidate)
                    queue.append(candidate)
        groups.append(sorted(group))
    return groups


def _planned_mosaic_bounds_wgs84(
    kml_paths: list[str | Path],
    source_dems: list[Path],
    buffer_m: float,
) -> tuple[float, float, float, float]:
    seed = _pick_mosaic_source(kml_paths, source_dems)
    with rasterio.open(seed) as src0:
        crs = src0.crs

    bounds = _bounds_union(
        [_descent_bounds_in_crs(kml_path, crs, buffer_m) for kml_path in kml_paths]
    )
    intersecting = []
    for path in source_dems:
        with rasterio.open(path) as src:
            if src.crs == crs and _intersects_bounds(path, bounds):
                intersecting.append(path)
    if not intersecting:
        raise ValueError(
            "no source DEM intersects buffered descent bounds for "
            f"{', '.join(map(str, kml_paths))}"
        )

    with rasterio.open(intersecting[0]) as src:
        res_x = abs(src.transform.a)
        res_y = abs(src.transform.e)

    width = math.ceil((bounds[2] - bounds[0]) / res_x)
    height = math.ceil((bounds[3] - bounds[1]) / res_y)
    snapped_bounds = (
        bounds[0],
        bounds[3] - height * res_y,
        bounds[0] + width * res_x,
        bounds[3],
    )
    return transform_bounds(crs, "EPSG:4326", *snapped_bounds, densify_pts=21)


def _merge_groups_by_planned_footprint(
    groups: list[list[str]],
    source_dems: list[Path],
    buffer_m: float,
) -> list[list[str]]:
    """Merge groups whose final pixel-snapped mosaic footprints would overlap."""
    groups = [sorted(group) for group in groups]
    while True:
        footprints = [
            box(*_planned_mosaic_bounds_wgs84(group, source_dems, buffer_m)) for group in groups
        ]
        merge_pair: tuple[int, int] | None = None
        for i, footprint_a in enumerate(footprints):
            for j in range(i + 1, len(footprints)):
                overlap = footprint_a.intersection(footprints[j])
                if not overlap.is_empty and overlap.area > 0:
                    merge_pair = (i, j)
                    break
            if merge_pair is not None:
                break
        if merge_pair is None:
            return groups

        i, j = merge_pair
        merged = sorted(set(groups[i]) | set(groups[j]))
        groups = [group for n, group in enumerate(groups) if n not in {i, j}]
        groups.append(merged)


def _download_name(url: str, fallback: str) -> str:
    name = Path(urlparse(url).path).name
    return name or fallback


def _inflate_wgs84_bounds(
    bounds: tuple[float, float, float, float], buffer_m: float
) -> tuple[float, float, float, float]:
    if buffer_m <= 0:
        return bounds
    minx, miny, maxx, maxy = bounds
    mean_lat_rad = math.radians(0.5 * (miny + maxy))
    dlat = buffer_m / 111_000.0
    dlon = buffer_m / (111_000.0 * max(math.cos(mean_lat_rad), 1e-6))
    return (minx - dlon, miny - dlat, maxx + dlon, maxy + dlat)


def _download_tnm_dems_for_kml(
    kml_path: str | Path, dem_dir: Path, *, buffer_m: float = 0.0
) -> list[Path]:
    descent_bounds = _descent_bounds_wgs84(kml_path)
    # Compact descents (e.g. <200 m wide) can return zero TNM matches when
    # queried with the raw bounds. Inflating by the mosaic buffer ensures
    # tiles that cover the surrounding AOI are returned.
    target_bounds = _inflate_wgs84_bounds(descent_bounds, buffer_m)
    items = query_tnm_products(target_bounds)
    candidates: list[dict[str, object]] = []
    for item in items:
        item_bounds = _extract_bounds(item)
        if item_bounds is not None and not _bounds_intersect(item_bounds, target_bounds):
            continue
        if _item_download_url(item) is None:
            continue
        candidates.append(item)

    candidates.sort(key=lambda item: str(item.get("title", "")))
    candidates.sort(key=lambda item: str(item.get("publicationDate", "")), reverse=True)
    if not candidates:
        raise ValueError(f"TNM returned no downloadable 1m DEM products for {kml_path}")

    downloaded: list[Path] = []
    for idx, item in enumerate(candidates):
        url = _item_download_url(item)
        assert url is not None
        fallback = f"{_safe_stem(kml_path)}_tnm_{idx}.tif"
        path = dem_dir / _download_name(url, fallback)
        if path.exists():
            log(f"DEM download hit: {path}")
        else:
            log(f"downloading DEM for {kml_path}: {url}")
            _download_file(url, path)
        downloaded.append(path)
    return downloaded


def _descent_coverage_counts(kml_path: str | Path, dem_path: str | Path) -> tuple[int, int]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError(f"KML has no descent lines: {kml_path}")

    with rasterio.open(dem_path) as src:
        descent = features.descent_lines.to_crs(src.crs)
        dem_bounds = box(*src.bounds)
        descent_bounds = box(*descent.total_bounds)
        if not dem_bounds.covers(descent_bounds):
            raise ValueError(f"DEM bounds do not fully cover descent bounds: {kml_path} -> {dem_path}")

        geoms = [
            (geom, 1)
            for geom in descent.geometry
            if isinstance(geom, (LineString, MultiLineString)) and not geom.is_empty
        ]
        if not geoms:
            raise ValueError(f"KML has no rasterizable descent geometries: {kml_path}")

        line_mask = rasterize(
            geoms,
            out_shape=(src.height, src.width),
            transform=src.transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True,
        )
        line_pixels = int(line_mask.sum())
        if line_pixels == 0:
            raise ValueError(f"descent line rasterizes to zero pixels: {kml_path} -> {dem_path}")

        arr = src.read(1, masked=True)
        mask = np.ma.getmaskarray(arr)
        values = np.asarray(arr.filled(np.nan), dtype=np.float32)
        valid_dem = (~mask) & np.isfinite(values)
        valid_line_pixels = int((valid_dem & (line_mask == 1)).sum())

    return valid_line_pixels, line_pixels


def _validate_descent_coverage(kml_path: str | Path, dem_path: str | Path) -> None:
    valid_line_pixels, line_pixels = _descent_coverage_counts(kml_path, dem_path)
    if valid_line_pixels != line_pixels:
        raise ValueError(
            f"DEM has nodata under descent track: {kml_path} -> {dem_path} "
            f"({valid_line_pixels}/{line_pixels} valid descent pixels)"
        )


def _pick_mosaic_source(kml_paths: list[str | Path], source_dems: list[Path]) -> Path:
    """Pick a source DEM whose CRS should be used for the mosaic.

    Source DEMs span multiple UTM zones (e.g. CA in zone 11, UT in zone 12). We must
    pick a CRS belonging to a DEM that actually overlaps a KML descent — using
    the alphabetical first source silently drops every DEM in a different zone.
    """
    descent_bounds = [_descent_bounds_wgs84(kml_path) for kml_path in kml_paths]
    for path in source_dems:
        with rasterio.open(path) as src:
            src_wgs = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        if any(_bounds_intersect(src_wgs, bounds) for bounds in descent_bounds):
            return path
    raise ValueError(f"no source DEM overlaps descent bounds for {', '.join(map(str, kml_paths))}")


def _write_kml_mosaic(
    kml_paths: list[str | Path],
    source_dems: list[Path],
    output_path: Path,
    buffer_m: float,
    max_pixels: int,
) -> tuple[int, tuple[int, int]]:
    if not source_dems:
        raise ValueError("source_dems must not be empty")
    if not kml_paths:
        raise ValueError("kml_paths must not be empty")

    group_label = ", ".join(str(path) for path in kml_paths)
    seed = _pick_mosaic_source(kml_paths, source_dems)
    with rasterio.open(seed) as src0:
        crs = src0.crs
        nodata = _safe_output_nodata(src0.nodata, src0.dtypes[0])

    bounds = _bounds_union(
        [_descent_bounds_in_crs(kml_path, crs, buffer_m) for kml_path in kml_paths]
    )
    same_crs = []
    for path in source_dems:
        with rasterio.open(path) as src:
            if src.crs == crs:
                same_crs.append(path)
    intersecting = [p for p in same_crs if _intersects_bounds(p, bounds)]
    if not intersecting:
        raise ValueError(f"no source DEM intersects buffered descent bounds for {group_label}")

    srcs = [rasterio.open(p) for p in intersecting]
    try:
        for src in srcs:
            if src.crs != crs:
                raise ValueError(f"source DEM CRS mismatch: {src.name} has {src.crs}, expected {crs}")
        res_x = abs(srcs[0].transform.a)
        res_y = abs(srcs[0].transform.e)
        width = math.ceil((bounds[2] - bounds[0]) / res_x)
        height = math.ceil((bounds[3] - bounds[1]) / res_y)
        pixels = width * height
        log(
            f"mosaic bounds for {group_label}: {width}x{height}={pixels:,} pixels, "
            f"sources={len(intersecting)}"
        )
        if pixels > max_pixels:
            raise ValueError(
                f"buffered mosaic would be too large for {group_label}: "
                f"{width}x{height}={pixels:,} pixels exceeds max_pixels={max_pixels:,}"
            )
        mosaic, transform = merge(srcs, bounds=bounds, nodata=nodata)
        data = mosaic[0]
        profile = srcs[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=data.shape[0],
            width=data.shape[1],
            count=1,
            transform=transform,
            crs=crs,
            nodata=nodata,
            dtype=data.dtype,
            compress="deflate",
            predictor=2,
            tiled=True,
            blockxsize=256,
            blockysize=256,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(data, 1)
    finally:
        for src in srcs:
            src.close()

    for kml_path in kml_paths:
        _validate_descent_coverage(kml_path, output_path)
    return len(intersecting), data.shape


def _group_stem(kml_paths: list[str | Path]) -> str:
    first = _safe_stem(kml_paths[0])
    if len(kml_paths) == 1:
        return first
    return f"{first}_plus_{len(kml_paths) - 1}"


def _validate_output_mosaics_disjoint(rows: list[dict[str, object]]) -> None:
    footprints = []
    for row in rows:
        dem_path = row["dem_path"]
        if not isinstance(dem_path, str):
            continue
        with rasterio.open(dem_path) as src:
            bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
        footprints.append((dem_path, box(*bounds)))

    for i, (path_a, footprint_a) in enumerate(footprints):
        for path_b, footprint_b in footprints[i + 1 :]:
            overlap = footprint_a.intersection(footprint_b)
            if not overlap.is_empty and overlap.area > 0:
                raise RuntimeError(
                    "grouped mosaic manifest produced overlapping DEM footprints: "
                    f"{path_a} overlaps {path_b}"
                )


def build_mosaic_manifest(
    manifest_path: str | Path,
    output_manifest_path: str | Path,
    *,
    dem_dir: str | Path,
    kml_dir: str | Path,
    output_dem_dir: str | Path,
    buffer_m: float = 2000.0,
    min_valid_fraction: float = 0.2,
    max_pixels: int = 120_000_000,
    download_missing: bool = False,
    overwrite: bool = False,
) -> list[dict[str, object]]:
    manifest_path = Path(manifest_path)
    output_manifest_path = Path(output_manifest_path)
    dem_dir = Path(dem_dir)
    kml_dir = Path(kml_dir)
    output_dem_dir = Path(output_dem_dir)

    rows = json.loads(manifest_path.read_text())
    if not isinstance(rows, list):
        raise ValueError("manifest must be a JSON list")

    source_dems = _source_dem_paths(dem_dir, output_dem_dir, min_valid_fraction)
    if not source_dems:
        raise ValueError(f"no source DEMs found under {dem_dir}")
    log(f"using {len(source_dems)} source DEMs for KML mosaics")

    kml_paths = _dedupe_manifest_kml_paths(rows, kml_dir)
    groups = _cluster_kml_paths(kml_paths, buffer_m)
    initial_group_count = len(groups)
    groups = _merge_groups_by_planned_footprint(groups, source_dems, buffer_m)
    log(
        f"grouped {len(kml_paths)} KMLs into {len(groups)} non-overlapping mosaic footprint(s) "
        f"(initial KML-bound groups={initial_group_count})"
    )

    new_rows: list[dict[str, object]] = []
    coverage_errors: list[str] = []
    for group in groups:
        group_label = ", ".join(group)
        out_dem = output_dem_dir / f"{_group_stem(group)}_buffer{int(buffer_m)}m.tif"
        is_ready = False
        try:
            if overwrite or not out_dem.exists():
                log(f"building mosaic for {group_label}")
                source_count, shape = _write_kml_mosaic(
                    group, source_dems, out_dem, buffer_m, max_pixels
                )
                log(f"mosaic ready: {out_dem} ({shape[1]}x{shape[0]}, sources={source_count})")
            else:
                for kml_path in group:
                    _validate_descent_coverage(kml_path, out_dem)
                log(f"mosaic hit: {out_dem}")
            is_ready = True
        except ValueError as exc:
            if not download_missing:
                coverage_errors.append(str(exc))
                log(f"mosaic invalid: {exc}")
                continue

            log(f"mosaic invalid, attempting DEM download repair: {exc}")
            try:
                for kml_path in group:
                    _download_tnm_dems_for_kml(kml_path, dem_dir, buffer_m=buffer_m)
                source_dems = _source_dem_paths(dem_dir, output_dem_dir, min_valid_fraction)
                source_count, shape = _write_kml_mosaic(
                    group, source_dems, out_dem, buffer_m, max_pixels
                )
                log(
                    f"mosaic repaired: {out_dem} "
                    f"({shape[1]}x{shape[0]}, sources={source_count})"
                )
                is_ready = True
            except ValueError as repair_exc:
                coverage_errors.append(str(repair_exc))
                log(f"mosaic repair failed: {repair_exc}")
                continue

        if not is_ready:
            continue
        new_rows.append({"kml_paths": group, "dem_path": str(out_dem)})

    if coverage_errors:
        detail = "\n  - ".join(coverage_errors)
        raise RuntimeError(
            f"{len(coverage_errors)} KML mosaic(s) do not have complete valid DEM coverage:\n"
            f"  - {detail}"
        )

    _validate_output_mosaics_disjoint(new_rows)
    output_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    output_manifest_path.write_text(json.dumps(new_rows, indent=2) + "\n")
    log(f"wrote {len(new_rows)} entries to {output_manifest_path}")
    return new_rows


def mosaic_manifest_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis mosaic-manifest",
        description="Build grouped buffered mosaic DEMs and write an updated training manifest.",
    )
    p.add_argument("manifest", type=Path)
    p.add_argument("output_manifest", type=Path)
    p.add_argument("--dem-dir", type=Path, default=Path("data/ropewiki/dem"))
    p.add_argument("--kml-dir", type=Path, default=Path("data/ropewiki/kml"))
    p.add_argument("--output-dem-dir", type=Path, default=Path("data/ropewiki/dem/mosaic"))
    p.add_argument("--buffer-m", type=float, default=2000.0)
    p.add_argument("--min-valid-fraction", type=float, default=0.2)
    p.add_argument("--max-pixels", type=int, default=120_000_000)
    p.add_argument(
        "--download-missing",
        action="store_true",
        help="Download all TNM 1m DEM products intersecting a KML descent when local mosaics lack valid coverage.",
    )
    p.add_argument("--overwrite", action="store_true")
    args = p.parse_args(argv)
    build_mosaic_manifest(
        args.manifest,
        args.output_manifest,
        dem_dir=args.dem_dir,
        kml_dir=args.kml_dir,
        output_dem_dir=args.output_dem_dir,
        buffer_m=args.buffer_m,
        min_valid_fraction=args.min_valid_fraction,
        max_pixels=args.max_pixels,
        download_missing=args.download_missing,
        overwrite=args.overwrite,
    )
