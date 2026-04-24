from __future__ import annotations

import geopandas as gpd
import numpy as np
from rasterio.features import geometry_mask
from rasterio.features import shapes as rio_shapes
from rasterio.transform import xy as rio_xy
from scipy import ndimage as ndi
from shapely.geometry import Point, mapping, shape

from .config import Config


def rappel_points_gdf(rappel_heights: np.ndarray, transform, crs) -> gpd.GeoDataFrame:
    mask = rappel_heights > 0
    labeled, n = ndi.label(mask, structure=np.ones((3, 3), dtype=int))
    if n == 0:
        return gpd.GeoDataFrame(columns=["drop_height_m", "geometry"], geometry="geometry", crs=crs)

    idx = np.arange(1, n + 1)
    max_heights = ndi.maximum(rappel_heights, labels=labeled, index=idx)
    centroids = ndi.center_of_mass(mask, labels=labeled, index=idx)

    rows = []
    for (cy, cx), h in zip(centroids, max_heights, strict=True):
        x, y = rio_xy(transform, round(cy), round(cx))
        rows.append({"drop_height_m": float(h), "geometry": Point(x, y)})

    return gpd.GeoDataFrame(rows, crs=crs)


def drainage_polys_gdf(
    tpi: np.ndarray,
    hand: np.ndarray,
    slope: np.ndarray,
    transform,
    crs,
    cfg: Config,
) -> gpd.GeoDataFrame:
    mask = (
        (tpi < cfg.tpi_canyon_thresh)
        & (hand < cfg.hand_threshold)
        & (slope > cfg.min_channel_slope)
    ).astype(np.uint8)

    geoms = [
        shape(geom)
        for geom, val in rio_shapes(mask, mask=mask.astype(bool), transform=transform)
        if val == 1
    ]
    if not geoms:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=crs)
    return gpd.GeoDataFrame({"geometry": geoms}, crs=crs)


def score_routes(
    drainage: gpd.GeoDataFrame,
    rappel_pts: gpd.GeoDataFrame,
    elev: np.ndarray,
    transform,
    cfg: Config,
) -> gpd.GeoDataFrame:
    if drainage.empty:
        return drainage.assign(total_descent_m=[], grade=[], cliff_count=[], length_proxy_m=[])

    h, w = elev.shape
    descents, grades, cliff_counts, length_proxies = [], [], [], []

    for poly in drainage.geometry:
        m = geometry_mask([mapping(poly)], out_shape=(h, w), transform=transform, invert=True)
        vals = elev[m]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            descents.append(0)
            grades.append(0)
            cliff_counts.append(0)
            length_proxies.append(0)
            continue

        descent = float(vals.max() - vals.min())
        length_proxy = poly.area**0.5
        grade = descent / max(length_proxy, 1.0)
        ccount = 0 if rappel_pts.empty else int(rappel_pts.geometry.within(poly.buffer(30)).sum())

        descents.append(descent)
        grades.append(grade)
        cliff_counts.append(ccount)
        length_proxies.append(length_proxy)

    scored = drainage.copy()
    scored["total_descent_m"] = descents
    scored["length_proxy_m"] = length_proxies
    scored["grade"] = grades
    scored["cliff_count"] = cliff_counts
    return scored
