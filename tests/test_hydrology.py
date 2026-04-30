from __future__ import annotations

import numpy as np
import pytest
from rasterio.transform import from_origin

from canyongis import hydrology


def test_compute_flow_accum_falls_back_when_priority_flood_count_fails(monkeypatch):
    original = hydrology.Grid.fill_depressions

    def fail_priority_flood(self, dem, *args, **kwargs):
        raise KeyError(
            "int64 generator(func=<function count at 0x0>, args=(int64, Literal[int](-1))"
        )

    monkeypatch.setattr(hydrology.Grid, "fill_depressions", fail_priority_flood)

    yy, xx = np.mgrid[:32, :32].astype(np.float32)
    elev = 100.0 - yy - 0.1 * xx
    transform = from_origin(0.0, 32.0, 1.0, 1.0)

    with pytest.warns(RuntimeWarning, match="falling back to pit-filled DEM"):
        acc = hydrology.compute_flow_accum(elev.astype(np.float32), transform)

    assert acc.shape == elev.shape
    assert np.isfinite(acc).all()

    monkeypatch.setattr(hydrology.Grid, "fill_depressions", original)


def test_compute_flow_accum_can_skip_fill_depressions(monkeypatch):
    def fail_if_called(self, dem, *args, **kwargs):
        raise AssertionError("fill_depressions should not be called")

    monkeypatch.setattr(hydrology.Grid, "fill_depressions", fail_if_called)

    yy, xx = np.mgrid[:32, :32].astype(np.float32)
    elev = 100.0 - yy - 0.1 * xx
    transform = from_origin(0.0, 32.0, 1.0, 1.0)

    acc = hydrology.compute_flow_accum(
        elev.astype(np.float32),
        transform,
        fill_depressions=False,
    )

    assert acc.shape == elev.shape
    assert np.isfinite(acc).all()


def test_compute_flow_accum_maps_nan_to_nodata_before_fill_depressions(monkeypatch):
    seen: dict[str, int | bool] = {}

    def inspect_fill_depressions(self, dem, *args, **kwargs):
        arr = np.asarray(dem)
        seen["has_nan"] = bool(np.isnan(arr).any())
        seen["nodata_count"] = int(np.sum(arr == -9999.0))
        return dem

    monkeypatch.setattr(hydrology.Grid, "fill_depressions", inspect_fill_depressions)

    yy, xx = np.mgrid[:32, :32].astype(np.float32)
    elev = 100.0 - yy - 0.1 * xx
    elev[8:12, 10:13] = np.nan
    transform = from_origin(0.0, 32.0, 1.0, 1.0)

    acc = hydrology.compute_flow_accum(elev.astype(np.float32), transform)

    assert acc.shape == elev.shape
    assert seen["has_nan"] is False
    assert seen["nodata_count"] >= 12
