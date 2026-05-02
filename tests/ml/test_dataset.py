from __future__ import annotations

import json
import os

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.ml.config import MLConfig
from canyongis.ml.dataset import (
    ManifestEntry,
    SpatialKFoldSplitter,
    TileRecord,
    _cache_worker_init,
    _prewarm_shared_hydrology_numba_cache,
    _resolved_cache_block_size,
    build_tile_index,
    make_weighted_sampler,
    prepare_cache,
    read_cache_feature_tile,
)
from canyongis.ml.features import feature_stack_signature


def _make_records(n_dems: int, tiles_per_dem: int = 20) -> list[TileRecord]:
    recs = []
    for d in range(n_dems):
        dem_id = f"dem{d:03d}"
        for t in range(tiles_per_dem):
            recs.append(
                TileRecord(
                    dem_id=dem_id,
                    row=t * 256,
                    col=0,
                    has_positive=False,
                )
            )
    return recs


def test_spatial_kfold_disjoint_val_sets():
    recs = _make_records(n_dems=40)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=42)
    folds = splitter.split(recs)
    val_union: set[int] = set()
    for train, val in folds:
        # No overlap within a fold.
        assert not (set(train) & set(val))
        # Train + val cover all records.
        assert set(train) | set(val) == set(range(len(recs)))
        val_union.update(val)
    # Every record is in exactly one val set.
    assert val_union == set(range(len(recs)))


def test_spatial_kfold_groups_dems():
    """Every tile from a given DEM must land in the same fold (no cross-canyon leakage)."""
    recs = _make_records(n_dems=20)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=7)
    folds = splitter.split(recs)
    rec_fold: dict[int, int] = {}
    for k, (_tr, val) in enumerate(folds):
        for i in val:
            rec_fold[i] = k
    by_dem: dict[str, set[int]] = {}
    for i, r in enumerate(recs):
        by_dem.setdefault(r.dem_id, set()).add(rec_fold[i])
    for dem_id, ks in by_dem.items():
        assert len(ks) == 1, f"DEM {dem_id} was split across folds: {ks}"


def test_spatial_kfold_balances_dem_counts():
    recs = _make_records(n_dems=95, tiles_per_dem=3)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=11)

    folds = splitter.split(recs)

    val_dem_counts = []
    for _train, val in folds:
        val_dem_counts.append(len({recs[i].dem_id for i in val}))
    assert val_dem_counts == [19, 19, 19, 19, 19]


def test_prepare_cache_masks_labels_outside_valid_dem(tmp_path):
    h = w = 64
    pixel = 1.0
    transform = from_origin(500000.0, 4100000.0 + h * pixel, pixel, pixel)
    crs = rasterio.crs.CRS.from_epsg(26911)
    elev = np.ones((h, w), dtype=np.float32) * 100.0
    elev[24:40, :] = np.nan

    dem_path = tmp_path / "dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        height=h,
        width=w,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(np.where(np.isnan(elev), -9999.0, elev).astype(np.float32), 1)

    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    col = w / 2
    line_utm = [(transform.c + col * pixel, transform.f - r * pixel) for r in range(4, h - 4)]
    line_wgs = [t.transform(x, y) for x, y in line_utm]
    coords = " ".join(f"{x},{y},0" for x, y in line_wgs)
    kml_path = tmp_path / "trace.kml"
    kml_path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Full Descent</name><LineString><coordinates>{coords}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )

    cache_dir = tmp_path / "cache"
    prepare_cache(
        [ManifestEntry(kml_path=str(kml_path), dem_path=str(dem_path))],
        cache_dir,
        MLConfig(cache_workers=1),
    )

    zarr_path = next(cache_dir.glob("*/cache.zarr"))
    meta_path = next(cache_dir.glob("*/meta.json"))
    import zarr

    root = zarr.open_group(zarr_path, mode="r")
    assert root["features"].shape == (MLConfig().in_channels, h, w)
    assert root["features"].chunks == (MLConfig().in_channels, 128, 128)
    assert root["canyon"].chunks == (128, 128)
    canyon = np.asarray(root["canyon"][:], dtype=np.float32)
    meta = json.loads(meta_path.read_text())
    assert meta["cache_storage"] == {
        "format": "zarr",
        "compress": "none",
        "feature_chunks": [MLConfig().in_channels, 128, 128],
        "label_chunks": [128, 128],
    }

    assert canyon[:24].max() > 0.0
    assert canyon[24:40].max() == 0.0
    assert canyon[40:].max() > 0.0

    tile_cfg = MLConfig(tile_size=16, train_stride=8)
    records = build_tile_index(cache_dir, tile_cfg)
    assert records
    tile = read_cache_feature_tile(cache_dir, records[0], tile_cfg)
    assert tile.shape == (tile_cfg.in_channels, tile_cfg.tile_size, tile_cfg.tile_size)


def test_prepare_cache_supports_geotiff_fallback(tmp_path):
    h = w = 32
    pixel = 1.0
    transform = from_origin(500000.0, 4100000.0 + h * pixel, pixel, pixel)
    crs = rasterio.crs.CRS.from_epsg(26911)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    elev = (100.0 - yy - 0.1 * xx).astype(np.float32)

    dem_path = tmp_path / "dem.tif"
    with rasterio.open(
        dem_path,
        "w",
        driver="GTiff",
        height=h,
        width=w,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=-9999.0,
    ) as dst:
        dst.write(elev, 1)

    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    line_utm = [
        (transform.c + 8 * pixel, transform.f - 4 * pixel),
        (transform.c + 8 * pixel, transform.f - 24 * pixel),
    ]
    line_wgs = [t.transform(x, y) for x, y in line_utm]
    coords = " ".join(f"{x},{y},0" for x, y in line_wgs)
    kml_path = tmp_path / "trace.kml"
    kml_path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Full Descent</name><LineString><coordinates>{coords}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )

    cfg = MLConfig(cache_workers=1, tile_size=16, train_stride=16, cache_format="geotiff")
    cache_dir = tmp_path / "cache"
    prepare_cache([ManifestEntry(kml_path=str(kml_path), dem_path=str(dem_path))], cache_dir, cfg)

    features_path = next(cache_dir.glob("*/features.tif"))
    canyon_path = next(cache_dir.glob("*/canyon_label.tif"))
    with rasterio.open(features_path) as src:
        assert src.count == cfg.in_channels
        assert src.compression is None
        assert src.block_shapes[0] == (16, 16)
    with rasterio.open(canyon_path) as src:
        assert src.compression is None
        assert src.block_shapes[0] == (16, 16)

    records = build_tile_index(cache_dir, cfg)
    assert records
    tile = read_cache_feature_tile(cache_dir, records[0], cfg)
    assert tile.shape == (cfg.in_channels, cfg.tile_size, cfg.tile_size)


def test_feature_stack_signature_changes_with_geomorphon_config():
    base = feature_stack_signature(MLConfig())
    geom = feature_stack_signature(MLConfig(use_geomorphons=True, geomorphon_search_m=(10.0, 30.0)))
    assert base != geom
    assert len(geom["channel_names"]) == 17


def test_prepare_cache_supports_multiple_workers(tmp_path):
    h = w = 32
    pixel = 1.0
    crs = rasterio.crs.CRS.from_epsg(26911)
    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    entries = []

    for i in range(2):
        x0 = 500000.0 + i * 1000.0
        transform = from_origin(x0, 4100000.0 + h * pixel, pixel, pixel)
        yy, xx = np.mgrid[:h, :w].astype(np.float32)
        elev = (100.0 - yy - 0.1 * xx).astype(np.float32)
        dem_path = tmp_path / f"dem{i}.tif"
        with rasterio.open(
            dem_path,
            "w",
            driver="GTiff",
            height=h,
            width=w,
            count=1,
            dtype="float32",
            crs=crs,
            transform=transform,
            nodata=-9999.0,
        ) as dst:
            dst.write(elev, 1)

        line_utm = [
            (transform.c + 8 * pixel, transform.f - 4 * pixel),
            (transform.c + 8 * pixel, transform.f - 24 * pixel),
        ]
        line_wgs = [t.transform(x, y) for x, y in line_utm]
        coords = " ".join(f"{x},{y},0" for x, y in line_wgs)
        kml_path = tmp_path / f"trace{i}.kml"
        kml_path.write_text(
            '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<Placemark><name>Full Descent</name><LineString><coordinates>{coords}"
            "</coordinates></LineString></Placemark></Document></kml>"
        )
        entries.append(ManifestEntry(kml_path=str(kml_path), dem_path=str(dem_path)))

    cache_dir = tmp_path / "cache"
    prepare_cache(entries, cache_dir, MLConfig(cache_workers=2, tile_size=16))

    assert len(list(cache_dir.glob("*/cache.zarr"))) == 2


def test_cache_worker_init_respects_existing_numba_cache_dir(monkeypatch, tmp_path):
    shared_cache = tmp_path / "numba-shared"
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(shared_cache))

    _cache_worker_init()

    assert os.environ["NUMBA_CACHE_DIR"] == str(shared_cache)
    assert shared_cache.exists()


def test_prewarm_shared_hydrology_numba_cache_uses_configured_cache(monkeypatch, tmp_path):
    shared_cache = tmp_path / "numba-shared"
    monkeypatch.setenv("NUMBA_CACHE_DIR", str(shared_cache))

    import canyongis.hydrology as hydrology

    call: dict[str, object] = {}

    def fake_compute_flow_accum(elev, transform, *, fill_depressions=True):
        call["shape"] = elev.shape
        call["fill_depressions"] = fill_depressions
        return np.zeros_like(elev, dtype=np.float32)

    monkeypatch.setattr(hydrology, "compute_flow_accum", fake_compute_flow_accum)

    _prewarm_shared_hydrology_numba_cache(MLConfig())

    assert shared_cache.exists()
    assert call == {"shape": (64, 64), "fill_depressions": True}


def test_make_weighted_sampler_can_limit_samples_per_epoch():
    records = [
        TileRecord("dem", 0, 0, False),
        TileRecord("dem", 0, 1, True),
        TileRecord("dem", 0, 2, False),
        TileRecord("dem", 0, 3, True),
    ]

    sampler = make_weighted_sampler(records, [0, 1, 2, 3], MLConfig(train_samples_per_epoch=2))

    assert sampler.num_samples == 2


def test_cache_block_size_auto_matches_tile_grid():
    assert _resolved_cache_block_size(MLConfig(tile_size=256, train_stride=128)) == 128
    assert _resolved_cache_block_size(MLConfig(tile_size=256, train_stride=256)) == 256
    assert _resolved_cache_block_size(MLConfig(tile_size=256, train_stride=64)) == 64
    assert (
        _resolved_cache_block_size(MLConfig(tile_size=256, train_stride=128, cache_block_size=256))
        == 256
    )
