from __future__ import annotations

import numpy as np

# pysheds 0.5 calls np.in1d, which was removed in NumPy 2.0. np.isin is the
# documented replacement with the same semantics for 1-D needles.
if not hasattr(np, "in1d"):
    np.in1d = np.isin  # type: ignore[attr-defined]

from pysheds.grid import Grid  # noqa: E402
from pysheds.view import Raster, ViewFinder  # noqa: E402
from scipy import ndimage as ndi  # noqa: E402


def compute_flow_accum(elev: np.ndarray, transform) -> np.ndarray:
    """D8 flow accumulation via pysheds (fill pits → fill depressions → resolve flats → D8)."""
    nodata = np.float32(-9999.0)
    data = np.where(np.isnan(elev), nodata, elev).astype(np.float32, copy=False)
    vf = ViewFinder(affine=transform, shape=data.shape, nodata=nodata)
    dem = Raster(data, viewfinder=vf)
    grid = Grid(viewfinder=vf)
    pit_filled = grid.fill_pits(dem)
    flooded = grid.fill_depressions(pit_filled)
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    return np.asarray(acc, dtype=np.float32)


def compute_hand(elev: np.ndarray, stream_mask: np.ndarray) -> np.ndarray:
    """Height Above Nearest Drainage via EDT."""
    iy, ix = ndi.distance_transform_edt(
        ~stream_mask, return_distances=False, return_indices=True
    )
    hand = (elev - elev[iy, ix]).astype(np.float32)
    hand[stream_mask] = 0.0
    return hand
