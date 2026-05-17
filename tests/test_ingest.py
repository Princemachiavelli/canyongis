from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import LineString

from canyongis.ingest import (
    TNM_1M_DATASET,
    TNM_UNAVAILABLE_MESSAGE,
    RopewikiPageRef,
    choose_tnm_product,
    dem_covers_bounds,
    dem_descent_coverage_counts,
    find_covering_dem,
    ingest_ropewiki_page,
    normalize_ropewiki_title,
    parse_ropewiki_ref,
    query_tnm_products,
)


def _write_kml(path: Path, line_name: str) -> tuple[float, float, float, float]:
    crs = rasterio.crs.CRS.from_epsg(26911)
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    line_proj = LineString([(500010, 4099968), (500050, 4099968)])
    line_wgs = LineString([t.transform(x, y) for x, y in line_proj.coords])
    bounds = tuple(float(v) for v in line_wgs.bounds)
    coords = " ".join(f"{x},{y},0" for x, y in line_wgs.coords)
    path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>{line_name}</name><LineString><coordinates>{coords}</coordinates></LineString></Placemark>"
        "</Document></kml>"
    )
    return bounds


def _write_dem(path: Path, *, nodata_under_descent: bool = False) -> None:
    arr = np.zeros((128, 128), dtype=np.float32)
    if nodata_under_descent:
        arr[30:35, 8:53] = -9999.0
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype="float32",
        crs="EPSG:26911",
        transform=from_origin(500000, 4100000, 1.0, 1.0),
        nodata=-9999.0,
    ) as dst:
        dst.write(arr, 1)


def test_normalize_ropewiki_title_accepts_url_and_title():
    assert normalize_ropewiki_title("Willow Creek Canyon") == "Willow_Creek_Canyon"
    assert (
        normalize_ropewiki_title("https://ropewiki.com/Willow_Creek_Canyon")
        == "Willow_Creek_Canyon"
    )
    assert (
        normalize_ropewiki_title("https://ropewiki.com/index.php?title=Willow_Creek_Canyon")
        == "Willow_Creek_Canyon"
    )


def test_parse_ropewiki_ref_accepts_page_ids():
    assert parse_ropewiki_ref("1431") == RopewikiPageRef(page_id=1431)
    assert parse_ropewiki_ref("https://ropewiki.com/index.php?curid=1431") == RopewikiPageRef(
        page_id=1431
    )


def test_dem_cover_helpers(tmp_path):
    dem_path = tmp_path / "tile.tif"
    _write_dem(dem_path)
    kml_path = tmp_path / "descent.kml"
    bounds_wgs84 = _write_kml(kml_path, "Full Descent")

    assert dem_covers_bounds(dem_path, bounds_wgs84)
    assert find_covering_dem([dem_path], bounds_wgs84) == dem_path


def test_find_covering_dem_rejects_nodata_under_descent(tmp_path):
    bad_dem = tmp_path / "bad.tif"
    good_dem = tmp_path / "good.tif"
    _write_dem(bad_dem, nodata_under_descent=True)
    _write_dem(good_dem)
    kml_path = tmp_path / "descent.kml"
    bounds_wgs84 = _write_kml(kml_path, "Full Descent")

    assert dem_covers_bounds(bad_dem, bounds_wgs84)
    assert dem_descent_coverage_counts(kml_path, bad_dem)[0] == 0
    assert find_covering_dem([bad_dem, good_dem], bounds_wgs84, kml_path) == good_dem


def test_choose_tnm_product_prefers_covering_bbox():
    target = (-116.0, 35.0, -115.9, 35.1)
    items = [
        {
            "title": "older",
            "downloadURL": "https://example.com/older.tif",
            "boundingBox": {"minX": -116.5, "minY": 34.5, "maxX": -116.1, "maxY": 35.5},
        },
        {
            "title": "covering",
            "downloadURL": "https://example.com/covering.tif",
            "boundingBox": {"minX": -116.1, "minY": 34.9, "maxX": -115.8, "maxY": 35.2},
        },
    ]

    chosen = choose_tnm_product(items, target)
    assert chosen["title"] == "covering"


def test_ingest_skips_kml_without_descent(monkeypatch, tmp_path):
    kml_path = tmp_path / "Approach_Only.kml"
    _write_kml(kml_path, "Approach")

    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Approach_Only.kml"}],
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "File:Approach_Only.kml",
                        "imageinfo": [{"url": "https://example.com/Approach_Only.kml"}],
                    }
                ]
            }
        }

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)
    monkeypatch.setattr(
        "canyongis.ingest._download_file", lambda url, path: path.write_bytes(kml_path.read_bytes())
    )

    result = ingest_ropewiki_page(
        "Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=None,
        cache_dirs=[],
    )

    assert result.status == "skipped_no_descent"
    assert result.dem_path is None
    assert not (tmp_path / "kml" / "Approach_Only.kml").exists()


def test_ingest_skips_when_kml_file_has_no_download_url(monkeypatch, tmp_path):
    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Broken.kml"}],
                        }
                    ]
                }
            }
        return {"query": {"pages": [{"title": "File:Broken.kml", "imageinfo": [{}]}]}}

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)

    result = ingest_ropewiki_page(
        "Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=None,
        cache_dirs=[],
    )

    assert result.status == "skipped_no_kml"
    assert result.kml_path is None
    assert result.dem_path is None


def test_ingest_reuses_existing_dem(monkeypatch, tmp_path):
    dem_path = tmp_path / "existing.tif"
    _write_dem(dem_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps([{"kml_path": "old.kml", "dem_path": str(dem_path)}]))

    source_kml = tmp_path / "Full_Descent.kml"
    _write_kml(source_kml, "Full Descent")

    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Full_Descent.kml"}],
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "File:Full_Descent.kml",
                        "imageinfo": [{"url": "https://example.com/Full_Descent.kml"}],
                    }
                ]
            }
        }

    def fail_tnm(bounds_wgs84):
        raise AssertionError("TNM should not be queried when an existing DEM covers the route")

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)
    monkeypatch.setattr(
        "canyongis.ingest._download_file",
        lambda url, path: path.write_bytes(source_kml.read_bytes()),
    )
    monkeypatch.setattr("canyongis.ingest.query_tnm_products", fail_tnm)

    result = ingest_ropewiki_page(
        "https://ropewiki.com/Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=manifest_path,
        cache_dirs=[],
    )

    assert result.status == "reused_dem"
    assert result.dem_path == str(dem_path)


def test_ingest_downloads_tnm_dem_when_not_cached(monkeypatch, tmp_path):
    source_kml = tmp_path / "Full_Descent.kml"
    _write_kml(source_kml, "Full Descent")

    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Full_Descent.kml"}],
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "File:Full_Descent.kml",
                        "imageinfo": [{"url": "https://example.com/Full_Descent.kml"}],
                    }
                ]
            }
        }

    items = [
        {
            "title": TNM_1M_DATASET,
            "downloadURL": "https://example.com/USGS_1M_tile.tif",
            "boundingBox": {"minX": -117.0, "minY": 34.0, "maxX": -115.0, "maxY": 36.0},
            "sourceId": "abc123",
        }
    ]

    downloads: list[Path] = []

    def fake_download(url, path):
        downloads.append(path)
        if path.suffix.lower() == ".kml":
            path.write_bytes(source_kml.read_bytes())
        else:
            _write_dem(path)

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)
    monkeypatch.setattr("canyongis.ingest._download_file", fake_download)
    monkeypatch.setattr("canyongis.ingest.query_tnm_products", lambda bounds_wgs84: items)

    result = ingest_ropewiki_page(
        "Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=None,
        cache_dirs=[],
    )

    assert result.status == "downloaded_dem"
    assert result.sciencebase_id == "abc123"
    assert result.dem_path == str(tmp_path / "dem" / "USGS_1M_tile.tif")
    assert (tmp_path / "dem" / "USGS_1M_tile.tif") in downloads


def test_ingest_tries_next_tnm_dem_when_download_has_nodata_under_descent(monkeypatch, tmp_path):
    source_kml = tmp_path / "Full_Descent.kml"
    _write_kml(source_kml, "Full Descent")

    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Full_Descent.kml"}],
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "File:Full_Descent.kml",
                        "imageinfo": [{"url": "https://example.com/Full_Descent.kml"}],
                    }
                ]
            }
        }

    items = [
        {
            "title": "bad",
            "downloadURL": "https://example.com/bad.tif",
            "boundingBox": {"minX": -117.0, "minY": 34.0, "maxX": -115.0, "maxY": 36.0},
            "publicationDate": "2024-01-01",
            "sourceId": "bad-id",
        },
        {
            "title": "good",
            "downloadURL": "https://example.com/good.tif",
            "boundingBox": {"minX": -117.0, "minY": 34.0, "maxX": -115.0, "maxY": 36.0},
            "publicationDate": "2023-01-01",
            "sourceId": "good-id",
        },
    ]

    downloads: list[Path] = []

    def fake_download(url, path):
        downloads.append(path)
        if path.suffix.lower() == ".kml":
            path.write_bytes(source_kml.read_bytes())
        elif path.name == "bad.tif":
            _write_dem(path, nodata_under_descent=True)
        else:
            _write_dem(path)

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)
    monkeypatch.setattr("canyongis.ingest._download_file", fake_download)
    monkeypatch.setattr("canyongis.ingest.query_tnm_products", lambda bounds_wgs84: items)

    result = ingest_ropewiki_page(
        "Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=None,
        cache_dirs=[],
    )

    assert result.status == "downloaded_dem"
    assert result.sciencebase_id == "good-id"
    assert result.dem_path == str(tmp_path / "dem" / "good.tif")
    assert tmp_path / "dem" / "bad.tif" in downloads
    assert tmp_path / "dem" / "good.tif" in downloads


def test_ingest_skips_when_tnm_has_no_1m_dem(monkeypatch, tmp_path):
    source_kml = tmp_path / "Full_Descent.kml"
    _write_kml(source_kml, "Full Descent")

    def fake_ropewiki(**params):
        if params["prop"] == "images":
            return {
                "query": {
                    "pages": [
                        {
                            "title": "Willow_Creek_Canyon",
                            "images": [{"title": "File:Full_Descent.kml"}],
                        }
                    ]
                }
            }
        return {
            "query": {
                "pages": [
                    {
                        "title": "File:Full_Descent.kml",
                        "imageinfo": [{"url": "https://example.com/Full_Descent.kml"}],
                    }
                ]
            }
        }

    monkeypatch.setattr("canyongis.ingest._ropewiki_query", fake_ropewiki)
    monkeypatch.setattr(
        "canyongis.ingest._download_file",
        lambda url, path: path.write_bytes(source_kml.read_bytes()),
    )
    monkeypatch.setattr("canyongis.ingest.query_tnm_products", lambda bounds_wgs84: [])

    result = ingest_ropewiki_page(
        "Willow_Creek_Canyon",
        kml_dir=tmp_path / "kml",
        dem_dir=tmp_path / "dem",
        manifest_path=None,
        cache_dirs=[],
    )

    assert result.status == "skipped_no_dem"
    assert result.dem_path is None


def test_query_tnm_products_reports_sciencebase_outage_payload(monkeypatch):
    monkeypatch.setattr(
        "canyongis.ingest._http_get_json",
        lambda url, params: {
            "errorMessage": "[BadRequest] '('Connection aborted.', RemoteDisconnected())'"
        },
    )

    with pytest.raises(RuntimeError, match="TNM/ScienceBase is currently unavailable") as exc:
        query_tnm_products((-112.99, 37.01, -112.93, 37.06))

    assert TNM_UNAVAILABLE_MESSAGE in str(exc.value)
    assert "RemoteDisconnected" in str(exc.value)


def test_query_tnm_products_reports_malformed_sciencebase_response(monkeypatch):
    def fail_json(url, params):
        raise json.JSONDecodeError("bad response", "{errorMessage=down}", 1)

    monkeypatch.setattr("canyongis.ingest._http_get_json", fail_json)

    with pytest.raises(RuntimeError, match="try the DEM mosaic job again later"):
        query_tnm_products((-112.99, 37.01, -112.93, 37.06))
