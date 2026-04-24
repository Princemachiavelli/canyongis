from __future__ import annotations

import geopandas as gpd
import numpy as np
from rasterio.features import geometry_mask
from shapely.geometry import mapping


def score_routes(
    drainage: gpd.GeoDataFrame,
    rappel_pts: gpd.GeoDataFrame,
    elev: np.ndarray,
    transform,
) -> gpd.GeoDataFrame:
    """Score drainage polygons by total descent, grade, and cliff proximity count.

    Generic over the source of `drainage` (polygons) and `rappel_pts` (points) —
    works with ML-derived centerlines buffered into polygons.
    """
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
