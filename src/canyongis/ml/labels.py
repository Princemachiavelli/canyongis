from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
from rasterio.features import rasterize
from scipy import ndimage as ndi
from shapely.geometry import LineString, MultiLineString, Point

from ..validate import parse_kml
from .config import MLConfig


def _rasterize_lines(
    lines_wgs84: gpd.GeoDataFrame,
    dem_transform,
    dem_crs,
    dem_shape: tuple[int, int],
) -> np.ndarray:
    """Return uint8 (H, W) mask: 1 on any polyline pixel, 0 elsewhere."""
    if lines_wgs84.empty:
        return np.zeros(dem_shape, dtype=np.uint8)
    lines_proj = lines_wgs84.to_crs(dem_crs)
    geoms = [
        (g, 1)
        for g in lines_proj.geometry
        if isinstance(g, (LineString, MultiLineString)) and not g.is_empty
    ]
    if not geoms:
        return np.zeros(dem_shape, dtype=np.uint8)
    return rasterize(
        geoms,
        out_shape=dem_shape,
        transform=dem_transform,
        fill=0,
        dtype=np.uint8,
        all_touched=True,
    )


def _rasterize_points(
    pts_wgs84: gpd.GeoDataFrame,
    dem_transform,
    dem_crs,
    dem_shape: tuple[int, int],
) -> np.ndarray:
    if pts_wgs84.empty:
        return np.zeros(dem_shape, dtype=np.uint8)
    pts_proj = pts_wgs84.to_crs(dem_crs)
    geoms = [(g, 1) for g in pts_proj.geometry if isinstance(g, Point) and not g.is_empty]
    if not geoms:
        return np.zeros(dem_shape, dtype=np.uint8)
    return rasterize(
        geoms,
        out_shape=dem_shape,
        transform=dem_transform,
        fill=0,
        dtype=np.uint8,
        all_touched=True,
    )


def soft_label_from_mask(
    mask: np.ndarray, pixel_size: float, sigma_m: float, floor: float
) -> np.ndarray:
    """Gaussian falloff from a binary seed mask.

    y = exp(-0.5 * (d / sigma_px)**2), thresholded below `floor` to 0.
    """
    if mask.sum() == 0:
        return np.zeros(mask.shape, dtype=np.float32)
    sigma_px = max(sigma_m / max(pixel_size, 1e-6), 1e-3)
    d = ndi.distance_transform_edt(mask == 0)
    y = np.exp(-0.5 * (d / sigma_px) ** 2).astype(np.float32)
    y[y < floor] = 0.0
    return y


def build_labels_from_kml(
    kml_path: str | Path,
    dem_transform,
    dem_crs,
    dem_shape: tuple[int, int],
    pixel_size: float,
    cfg: MLConfig,
    *,
    label_sigma_m: float | None = None,
    rappel_sigma_m: float | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Produce (canyon_soft_label, rappel_heatmap) from a KML aligned to a DEM.

    - Descent lines (from `validate.parse_kml`) → rasterized → distance-transform
      Gaussian soft labels with configurable `label_sigma_m`.
    - Rappel/drop-in points (from `features.rappel_points()`) → separate Gaussian
      heatmap with `rappel_sigma_m`.
    """
    features = parse_kml(kml_path)

    line_mask = _rasterize_lines(features.descent_lines, dem_transform, dem_crs, dem_shape)
    canyon_y = soft_label_from_mask(
        line_mask,
        pixel_size,
        sigma_m=label_sigma_m if label_sigma_m is not None else cfg.label_sigma_m,
        floor=cfg.label_floor,
    )

    pt_mask = _rasterize_points(features.rappel_points(), dem_transform, dem_crs, dem_shape)
    rappel_y = soft_label_from_mask(
        pt_mask,
        pixel_size,
        sigma_m=rappel_sigma_m if rappel_sigma_m is not None else cfg.rappel_sigma_m,
        floor=cfg.label_floor,
    )
    return canyon_y, rappel_y
