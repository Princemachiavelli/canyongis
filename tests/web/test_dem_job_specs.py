from __future__ import annotations

from canyongis.web.dem_jobs import get_spec, normalize_result


def test_tpi_job_spec_normalizes_outputs_and_layers() -> None:
    spec = get_spec("tpi-ravine")
    assert spec is not None
    result = {
        "dem_path": "/tmp/dem.tif",
        "out_dir": "/tmp/out",
        "bounds_wgs84": (-113.0, 37.0, -112.0, 38.0),
        "b_tpi_path": "/tmp/out/b_tpi.tif",
        "panel_b_tpi_url": "/outputs/out/panel_b_tpi.webp",
        "panel_b_tpi_pmtiles_path": "/tmp/out/panel_b_tpi.pmtiles",
        "panel_b_tpi_pmtiles_url": "/outputs/out/panel_b_tpi.pmtiles",
        "panel_b_tpi_tile_min_zoom": 10,
        "panel_b_tpi_tile_max_zoom": 14,
        "meta": {"display_hi": 3.0, "combine": "normalized_sum"},
    }

    normalized = normalize_result(spec, result)

    assert normalized["schema_version"] == 1
    assert normalized["outputs"] == [
        {
            "key": "b_tpi",
            "label": "TPI ravine score raster",
            "role": "ravine-score",
            "asset_kind": "raster",
            "asset_type": "raster.score.ravine.tpi",
            "path": "/tmp/out/b_tpi.tif",
            "metadata": {
                "result_key": "b_tpi_path",
                "role": "ravine-score",
                "display_hi": 3.0,
                "combine": "normalized_sum",
            },
            "bounds_wgs84": [-113.0, 37.0, -112.0, 38.0],
        }
    ]
    assert normalized["layers"] == [
        {
            "id": "panel_b_tpi",
            "key": "panel_b_tpi",
            "label": "TPI ravines",
            "kind": "raster-overlay",
            "source": {
                "type": "pmtiles",
                "url": "/outputs/out/panel_b_tpi.pmtiles",
                "path": "/tmp/out/panel_b_tpi.pmtiles",
                "min_zoom": 10,
                "max_zoom": 14,
            },
            "opacity": 0.72,
            "role": "ravine-score",
            "bounds_wgs84": [-113.0, 37.0, -112.0, 38.0],
        }
    ]


def test_normalize_result_preserves_existing_metadata() -> None:
    spec = get_spec("hillshade")
    assert spec is not None
    result = {"schema_version": 99, "outputs": [{"key": "custom"}], "layers": []}

    normalized = normalize_result(spec, result)

    assert normalized == result
