from __future__ import annotations

import os
import tempfile
import warnings

import numpy as np

os.environ.setdefault(
    "NUMBA_CACHE_DIR",
    os.path.join(tempfile.gettempdir(), f"canyongis-numba-cache-{os.getpid()}"),
)

# pysheds 0.5 calls np.in1d, which was removed in NumPy 2.0. np.isin is the
# documented replacement with the same semantics for 1-D needles.
if not hasattr(np, "in1d"):
    np.in1d = np.isin  # type: ignore[attr-defined]

from pysheds.grid import Grid
from pysheds.view import Raster, ViewFinder
from scipy import ndimage as ndi


def _is_priority_flood_count_keyerror(exc: Exception) -> bool:
    return isinstance(exc, KeyError) and "generator(func=<function count" in str(exc)


def compute_flow_accum(
    elev: np.ndarray,
    transform,
    *,
    fill_depressions: bool = True,
) -> np.ndarray:
    """D8 flow accumulation via pysheds (fill pits → fill depressions → resolve flats → D8)."""
    nodata = np.float32(-9999.0)
    data = np.where(np.isnan(elev), nodata, elev).astype(np.float32, copy=False)
    vf = ViewFinder(affine=transform, shape=data.shape, nodata=nodata)
    dem = Raster(data, viewfinder=vf)
    grid = Grid(viewfinder=vf)
    pit_filled = grid.fill_pits(dem)
    if fill_depressions:
        try:
            flooded = grid.fill_depressions(pit_filled)
        except KeyError as exc:
            if not _is_priority_flood_count_keyerror(exc):
                raise
            warnings.warn(
                "pysheds fill_depressions failed in numba priority-flood; "
                "falling back to pit-filled DEM for flow accumulation. "
                "Use --no-flow-fill-depressions to make this explicit.",
                RuntimeWarning,
                stacklevel=2,
            )
            flooded = pit_filled
    else:
        flooded = pit_filled
    inflated = grid.resolve_flats(flooded)
    fdir = grid.flowdir(inflated)
    acc = grid.accumulation(fdir)
    return np.asarray(acc, dtype=np.float32)


def compute_hand(elev: np.ndarray, stream_mask: np.ndarray) -> np.ndarray:
    """Height Above Nearest Drainage via EDT."""
    iy, ix = ndi.distance_transform_edt(~stream_mask, return_distances=False, return_indices=True)
    hand = (elev - elev[iy, ix]).astype(np.float32)
    hand[stream_mask] = 0.0
    return hand
