from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from canyongis.geomorphons import export_geomorphons


def test_export_geomorphons_runs_grass_quietly_by_default(monkeypatch, tmp_path):
    dem_path = tmp_path / "dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        height=8,
        width=8,
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=from_origin(500000.0, 4100000.0, 1.0, 1.0),
    ) as dst:
        dst.write(np.ones((8, 8), dtype=np.float32), 1)

    calls: list[dict[str, object]] = []

    def fake_run(cmd, **kwargs):
        params_path = Path(cmd[-1])
        calls.append(
            {
                "cmd": cmd,
                "kwargs": kwargs,
                "params": json.loads(params_path.read_text()),
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    export_geomorphons(dem_path, tmp_path / "geomorphons", outputs=("forms",))

    assert len(calls) == 1
    call = calls[0]
    assert call["params"]["quiet"] is True
    assert call["kwargs"]["stdout"] == subprocess.PIPE
    assert call["kwargs"]["stderr"] == subprocess.PIPE
    assert call["kwargs"]["env"]["GRASS_VERBOSE"] == "0"
