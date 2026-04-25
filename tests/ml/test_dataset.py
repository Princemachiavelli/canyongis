from __future__ import annotations

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.ml.config import MLConfig
from canyongis.ml.dataset import ManifestEntry, SpatialKFoldSplitter, TileRecord, prepare_cache


def _make_records(n_super: int, n_folds_worth: int = 5) -> list[TileRecord]:
    recs = []
    for s in range(n_super):
        super_id = f"demA:{s // 4}:{s % 4}"
        for t in range(n_folds_worth):
            recs.append(
                TileRecord(
                    dem_id="demA", row=s * 256, col=t * 128, super_tile_id=super_id, has_positive=False
                )
            )
    return recs


def test_spatial_kfold_disjoint_val_sets():
    recs = _make_records(n_super=40)
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


def test_spatial_kfold_groups_supertiles():
    """All records with the same super_tile_id must land in the same fold."""
    recs = _make_records(n_super=20)
    splitter = SpatialKFoldSplitter(n_folds=5, seed=7)
    folds = splitter.split(recs)
    # Reverse map: record index -> fold k (via val membership).
    rec_fold: dict[int, int] = {}
    for k, (_tr, val) in enumerate(folds):
        for i in val:
            rec_fold[i] = k
    # Group by super_tile_id.
    by_super: dict[str, set[int]] = {}
    for i, r in enumerate(recs):
        by_super.setdefault(r.super_tile_id, set()).add(rec_fold[i])
    for sid, ks in by_super.items():
        assert len(ks) == 1, f"super-tile {sid} was split across folds: {ks}"


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

    canyon_path = next(cache_dir.glob("*/canyon_label.tif"))
    with rasterio.open(canyon_path) as src:
        canyon = src.read(1)

    assert canyon[:24].max() > 0.0
    assert canyon[24:40].max() == 0.0
    assert canyon[40:].max() > 0.0
