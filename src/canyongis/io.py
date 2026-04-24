from __future__ import annotations

import html
import warnings
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio


def log(msg: str) -> None:
    print(f"[canyon] {msg}", flush=True)


def read_dem(path: str):
    """Return (elevation, transform, crs, pixel_size)."""
    with rasterio.open(path) as src:
        elev = src.read(1).astype(np.float32)
        nodata = src.nodata
        if nodata is not None:
            elev[elev == nodata] = np.nan
        transform = src.transform
        crs = src.crs
        pixel_size = abs(transform.a)
    log(f"DEM loaded: shape={elev.shape}, pixel={pixel_size:.2f}m, crs={crs}")
    if not crs.is_projected:
        warnings.warn(
            "DEM is in a geographic CRS. Meter-based parameters will behave unpredictably. "
            "Reproject to UTM first: gdalwarp -t_srs EPSG:32612 in.tif out.tif",
            stacklevel=2,
        )
    return elev, transform, crs, pixel_size


def _write_kml_lines(gdf: gpd.GeoDataFrame, path: Path, *, color: str = "ff00ff00", width: int = 6) -> None:
    """Write a simple KML for line features with explicit style width."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        f"<Style id=\"lineStyle\"><LineStyle><color>{color}</color><width>{width}</width></LineStyle></Style>",
    ]
    gdf_wgs84 = gdf.to_crs("EPSG:4326")

    def _line_label(row: gpd.GeoSeries, idx: int) -> str:
        name = row.get("name")
        if name is not None and str(name).strip():
            return str(name)

        mean_prob = row.get("mean_prob")
        length_m = row.get("length_m")
        if mean_prob is not None and length_m is not None:
            return f"CL {idx + 1} (p={float(mean_prob):.2f}, len={float(length_m):.0f}m)"
        if mean_prob is not None:
            return f"CL {idx + 1} (p={float(mean_prob):.2f})"
        if length_m is not None:
            return f"CL {idx + 1} (len={float(length_m):.0f}m)"
        return f"Line {idx + 1}"

    for idx, (_, row) in enumerate(gdf_wgs84.iterrows()):
        geom = row.geometry
        name = html.escape(_line_label(row, idx))
        if geom.geom_type == "LineString":
            parts = [geom]
        elif geom.geom_type == "MultiLineString":
            parts = list(geom.geoms)
        else:
            continue
        for part in parts:
            coords = " ".join(f"{x},{y},0" for x, y in part.coords)
            lines.append(
                f"<Placemark><name>{name}</name><styleUrl>#lineStyle</styleUrl>"
                f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString>"
                f"</Placemark>"
            )
    lines.append("</Document></kml>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_vector(gdf: gpd.GeoDataFrame, base_path) -> None:
    """Write a GeoDataFrame as GPKG (native CRS) and KML (WGS84, for Google Earth)."""
    base = Path(base_path)
    if gdf.empty:
        log(f"  skipping empty layer: {base.name}")
        return
    gdf.to_file(base.with_suffix(".gpkg"), driver="GPKG")
    geom_types = set(gdf.geometry.geom_type.unique())
    if geom_types.issubset({"LineString", "MultiLineString"}):
        _write_kml_lines(gdf, base.with_suffix(".kml"))
        return
    try:
        import fiona

        fiona.supported_drivers["KML"] = "rw"
    except Exception:
        pass
    try:
        gdf.to_crs("EPSG:4326").to_file(base.with_suffix(".kml"), driver="KML")
    except Exception as e:
        log(f"  WARNING: KML write failed for {base.name}: {e}")


def write_raster(path: str, arr: np.ndarray, transform, crs, dtype=None) -> None:
    if dtype is None:
        dtype = arr.dtype
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=dtype,
        crs=crs,
        transform=transform,
        compress="deflate",
        predictor=2,
        tiled=True,
    ) as dst:
        dst.write(arr.astype(dtype), 1)
