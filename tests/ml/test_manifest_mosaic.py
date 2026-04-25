from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from affine import Affine
from pyproj import Transformer

from canyongis.ml.manifest_mosaic import build_mosaic_manifest


def _write_dem(path: Path, x0: float, value: float, *, nodata_line: bool = False) -> None:
    arr = np.full((64, 64), value, dtype=np.float32)
    if nodata_line:
        arr[30:35, 30:50] = -9999.0
    transform = Affine.translation(x0, 64.0) * Affine.scale(1.0, -1.0)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:32611",
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)


def _write_kml(path: Path) -> None:
    # This line crosses the boundary between the two synthetic DEMs.
    transformer = Transformer.from_crs("EPSG:32611", "EPSG:4326", always_xy=True)
    p0 = transformer.transform(500040.0, 32.0)
    p1 = transformer.transform(500088.0, 32.0)
    path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>Descent</name><LineString><coordinates>"
        f"{p0[0]},{p0[1]} {p1[0]},{p1[1]}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )


def test_build_mosaic_manifest_uses_multiple_dems_for_one_kml(tmp_path):
    dem_dir = tmp_path / "dem"
    kml_dir = tmp_path / "kml"
    out_dem_dir = dem_dir / "mosaic"
    dem_dir.mkdir()
    kml_dir.mkdir()

    _write_dem(dem_dir / "left.tif", 500000.0, 10.0)
    _write_dem(dem_dir / "right.tif", 500064.0, 20.0)
    kml_path = kml_dir / "Crossing.kml"
    _write_kml(kml_path)

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            [
                {"kml_path": str(kml_path), "dem_path": str(dem_dir / "left.tif")},
                {"kml_path": str(kml_path), "dem_path": str(dem_dir / "right.tif")},
            ]
        )
    )
    out_manifest = tmp_path / "mosaic-manifest.json"

    rows = build_mosaic_manifest(
        manifest_path,
        out_manifest,
        dem_dir=dem_dir,
        kml_dir=kml_dir,
        output_dem_dir=out_dem_dir,
        buffer_m=20.0,
    )

    assert len(rows) == 1
    out_dem = Path(rows[0]["dem_path"])
    assert out_dem.exists()
    assert json.loads(out_manifest.read_text()) == rows

    with rasterio.open(out_dem) as src:
        arr = src.read(1, masked=True)
        values = set(np.unique(arr.compressed()).tolist())
        assert 10.0 in values
        assert 20.0 in values
        assert src.width < 128
        assert src.height < 64


def test_build_mosaic_manifest_rejects_nodata_under_descent(tmp_path):
    dem_dir = tmp_path / "dem"
    kml_dir = tmp_path / "kml"
    out_dem_dir = dem_dir / "mosaic"
    dem_dir.mkdir()
    kml_dir.mkdir()

    _write_dem(dem_dir / "bad.tif", 500000.0, 10.0, nodata_line=True)

    transformer = Transformer.from_crs("EPSG:32611", "EPSG:4326", always_xy=True)
    p0 = transformer.transform(500030.0, 32.0)
    p1 = transformer.transform(500050.0, 32.0)
    kml_path = kml_dir / "Nodata.kml"
    kml_path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        "<Placemark><name>Descent</name><LineString><coordinates>"
        f"{p0[0]},{p0[1]} {p1[0]},{p1[1]}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )

    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([{"kml_path": str(kml_path), "dem_path": "ignored.tif"}]))

    with pytest.raises(RuntimeError, match="complete valid DEM coverage"):
        build_mosaic_manifest(
            manifest_path,
            tmp_path / "out.json",
            dem_dir=dem_dir,
            kml_dir=kml_dir,
            output_dem_dir=out_dem_dir,
            buffer_m=10.0,
        )


def test_build_mosaic_manifest_downloads_missing_dem_for_complete_coverage(monkeypatch, tmp_path):
    dem_dir = tmp_path / "dem"
    kml_dir = tmp_path / "kml"
    out_dem_dir = dem_dir / "mosaic"
    dem_dir.mkdir()
    kml_dir.mkdir()

    _write_dem(dem_dir / "left.tif", 500000.0, 10.0)
    kml_path = kml_dir / "Crossing.kml"
    _write_kml(kml_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([{"kml_path": str(kml_path), "dem_path": "ignored.tif"}]))

    monkeypatch.setattr(
        "canyongis.ml.manifest_mosaic.query_tnm_products",
        lambda bounds: [
            {
                "title": "right tile",
                "downloadURL": "https://example.com/right.tif",
            }
        ],
    )

    downloads: list[Path] = []

    def fake_download(url: str, path: Path) -> None:
        downloads.append(path)
        _write_dem(path, 500064.0, 20.0)

    monkeypatch.setattr("canyongis.ml.manifest_mosaic._download_file", fake_download)

    rows = build_mosaic_manifest(
        manifest_path,
        tmp_path / "out.json",
        dem_dir=dem_dir,
        kml_dir=kml_dir,
        output_dem_dir=out_dem_dir,
        buffer_m=20.0,
        download_missing=True,
    )

    assert downloads == [dem_dir / "right.tif"]
    assert len(rows) == 1
    with rasterio.open(rows[0]["dem_path"]) as src:
        arr = src.read(1, masked=True)
        values = set(np.unique(arr.compressed()).tolist())
        assert 10.0 in values
        assert 20.0 in values
