from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import canyongis.web.analysis_jobs  # noqa: F401  (registers analysis job specs on import)
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


def test_rvt_vat_registers_as_base_raster_layer() -> None:
    spec = get_spec("rvt")
    assert spec is not None
    vat_layer = next((L for L in spec.layers if L.key == "panel_rvt_vat"), None)
    assert vat_layer is not None, "VAT layer should be registered under the rvt job"
    assert vat_layer.kind == "base-raster"
    assert vat_layer.role == "base"
    slope_layer = next(L for L in spec.layers if L.key == "panel_rvt_slope")
    assert slope_layer.kind == "raster-overlay"
    assert slope_layer.role == "relief-visualization"
    assert any(v.key == "vat" for v in spec.visualizations)


def test_geonet_baseline_spec_matches_ravine_spine_asset_types() -> None:
    spec = get_spec("geonet-baseline")
    assert spec is not None
    assert spec.runner_kind == "context"
    assert [inp.kind for inp in spec.inputs] == ["raster.dem"]

    spine_spec = get_spec("ravine-spine")
    assert spine_spec is not None
    # Outputs must carry the same asset taxonomy as "Ravine spine from TPI"
    # so the two methods group and compare identically in the UI.
    assert {o.asset_type for o in spec.outputs} == {o.asset_type for o in spine_spec.outputs}
    assert {o.role for o in spec.outputs} == {o.role for o in spine_spec.outputs}


def test_geonet_baseline_runner_produces_spine_assets(tmp_path) -> None:
    import numpy as np
    from rasterio.crs import CRS
    from rasterio.transform import from_origin

    from canyongis.io import write_raster
    from canyongis.web import analysis_jobs

    # South-sloping valley with a 15 m V-incision along x=80 (see tests/test_baseline.py).
    h, w = 220, 160
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    elev = 300.0 - 0.2 * yy + 0.3 * np.abs(xx - 80.0)
    elev -= np.clip(15.0 - 2.5 * np.abs(xx - 80.0), 0.0, None)
    dem_path = tmp_path / "dem.tif"
    write_raster(
        dem_path,
        elev.astype(np.float32),
        from_origin(500_000.0, 4_000_000.0, 1.0, 1.0),
        CRS.from_epsg(32612),
    )

    dem_job = SimpleNamespace(
        status="succeeded",
        result={"dem_path": str(dem_path), "bounds_wgs84": None},
    )
    ctx = SimpleNamespace(
        output_root=tmp_path / "outputs",
        output_url=lambda p: f"/outputs/{Path(p).name}",
        job_or_404=lambda job_id: dem_job,
        now=lambda: "2026-06-09T00:00:00",
    )
    req = analysis_jobs.GeonetBaselineRequest(
        dem_job_id="dem-1",
        min_area_m2=2000.0,
        pm_iters=30,
        depth_radius_m=40.0,
        confine_depth_m=4.0,
        min_length_m=40.0,
    )

    result = analysis_jobs._run_geonet_baseline(
        ctx=ctx, req=req, inputs={}, progress_cb=lambda _msg: None
    )

    assert result["n_centerlines"] >= 1
    assert Path(result["centerlines_kml_path"]).exists()
    assert result["centerlines_kml_url"]
    assert result["bounds_wgs84"] is not None
    assert any(layer["key"] == "centerlines_layer" for layer in result["layers"])

    spec = get_spec("geonet-baseline")
    assert spec is not None
    normalized = normalize_result(spec, result)
    outputs = {o["key"]: o for o in normalized["outputs"]}
    assert outputs["centerlines"]["asset_type"] == "vector.kml.spine"


def test_kml_import_output_uses_route_name_as_label() -> None:
    spec = get_spec("kml-import")
    assert spec is not None
    result = {
        "name": "Fantastic Mr. Fox Canyon",
        "kml_path": "/tmp/out/fantastic.kml",
        "kml_url": "/outputs/fantastic.kml",
        "bounds_wgs84": [-113.0, 37.0, -112.0, 38.0],
    }

    normalized = normalize_result(spec, result)

    assert normalized["outputs"][0]["label"] == "Fantastic Mr. Fox Canyon"
