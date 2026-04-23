from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi
from pysheds.grid import Grid
from pysheds.view import Raster, ViewFinder


def compute_flow_accum(elev: np.ndarray, transform) -> np.ndarray:
    """D8 flow accumulation via pysheds (fill pits → fill depressions → resolve flats → D8)."""
    nodata = -9999.0
    data = np.where(np.isnan(elev), nodata, elev).astype(np.float64)
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
    _, (iy, ix) = ndi.distance_transform_edt(
        ~stream_mask, return_distances=True, return_indices=True
    )
    hand = (elev - elev[iy, ix]).astype(np.float32)
    hand[stream_mask] = 0.0
    return hand
