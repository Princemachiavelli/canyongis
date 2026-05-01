from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.drop_preview import drop_to_lower_ground, export_drop_previews


def test_drop_to_lower_ground_uses_local_minimum():
    elev = np.array(
        [
            [10.0, 10.0, 10.0],
            [10.0, 20.0, 10.0],
            [10.0, 10.0, 10.0],
        ],
        dtype=np.float32,
    )

    drop = drop_to_lower_ground(elev, pixel_size=1.0, distance_m=1.0)

    assert drop[1, 1] == 10.0
    assert drop[0, 0] == 0.0


def _write_dem(path: Path) -> None:
    yy, xx = np.mgrid[:64, :64].astype(np.float32)
    elev = 200.0 - yy * 0.5
    elev[:, 35:] -= 20.0
    elev += xx * 0.02
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=elev.shape[0],
        width=elev.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:32611",
        transform=from_origin(500000.0, 64.0, 1.0, 1.0),
        nodata=-9999.0,
    ) as dst:
        dst.write(elev, 1)


def _write_kml(path: Path) -> None:
    transformer = Transformer.from_crs("EPSG:32611", "EPSG:4326", always_xy=True)
    p0 = transformer.transform(500030.0, 55.0)
    p1 = transformer.transform(500040.0, 5.0)
    path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>Descent</name><LineString><coordinates>"
        f"{p0[0]},{p0[1]} {p1[0]},{p1[1]}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )


def test_export_drop_previews_writes_unified_outputs(tmp_path):
    dem = tmp_path / "dem.tif"
    kml = tmp_path / "route.kml"
    out_dir = tmp_path / "drop"
    _write_dem(dem)
    _write_kml(kml)

    summary = export_drop_previews(
        dem,
        kml,
        out_dir,
        distances_m=(2.0, 5.0),
        crop_buffer_m=20.0,
    )

    assert Path(summary["comparison_png"]).exists()
    assert Path(summary["log_comparison_png"]).exists()
    assert Path(summary["stats_json"]).exists()
    assert len(summary["results"]) == 2
    for result in summary["results"]:
        assert Path(result["raster_path"]).exists()
        assert Path(result["preview_path"]).exists()
        assert Path(result["log_preview_path"]).exists()
        assert result["route_quantiles_m"]

    with Image.open(summary["comparison_png"]) as image:
        assert image.width > 0
        assert image.height > 0

    stats = json.loads(Path(summary["stats_json"]).read_text())
    assert stats["distances_m"] == [2.0, 5.0]
