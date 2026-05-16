from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest

import canyongis.__main__ as main_mod


@pytest.mark.parametrize(
    ("command", "attr"),
    [
        ("train", "train_cli"),
        ("predict", "predict_cli"),
        ("export-features", "export_feature_pngs_cli"),
        ("ropewiki-sync", "ingest_ropewiki_cli"),
        ("mosaic-manifest", "mosaic_manifest_cli"),
        ("geomorphons", "geomorphons_cli"),
        ("geomorphon-previews", "geomorphon_previews_cli"),
        ("drop-previews", "drop_previews_cli"),
        ("kml-snap", "kml_snap_cli"),
        ("track-profile", "track_profile_cli"),
        ("drainage-previews", "drainage_previews_cli"),
        ("ravine-spine", "ravine_spine_cli"),
        ("web", "web_cli"),
    ],
)
def test_cli_dispatches_subcommands(monkeypatch: pytest.MonkeyPatch, command: str, attr: str) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(main_mod, attr, lambda argv: calls.append(argv))

    main_mod.cli([command, "--flag", "value"])

    assert calls == [["--flag", "value"]]


def test_cli_default_pipeline_builds_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []
    monkeypatch.setattr(main_mod, "main", lambda cfg, render_folium: calls.append((cfg, render_folium)))

    main_mod.cli(["dem.tif", "best.pt", str(tmp_path / "out"), "--no-folium"])

    cfg, render_folium = calls[0]
    assert cfg.dem_path == "dem.tif"
    assert cfg.checkpoint_path == "best.pt"
    assert cfg.out_dir == str(tmp_path / "out")
    assert cfg.use_geomorphons is True
    assert cfg.render_folium is False
    assert render_folium is False


def test_cli_errors_without_required_default_args() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main_mod.cli(["dem.tif"])

    assert excinfo.value.code == 2


def test_main_runs_inference_and_optional_map(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []
    cfg = main_mod.MLConfig(dem_path="dem.tif", out_dir=str(tmp_path / "out"))
    monkeypatch.setattr(main_mod, "log", events.append)
    monkeypatch.setattr(main_mod, "run_inference", lambda got: events.append(got.dem_path))
    monkeypatch.setattr(main_mod, "_render_map", lambda *args: events.append("render"))

    main_mod.main(cfg, render_folium=True)

    assert events == ["STEP 1: ML inference", "dem.tif", "STEP 2: render Folium map", "render", "Done."]
    assert Path(cfg.out_dir).is_dir()


def test_render_map_reads_outputs_and_dem_bounds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "canyon_centerlines.gpkg").touch()
    (out_dir / "rappel_points.gpkg").touch()
    reads: list[Path] = []
    rendered = []

    def fake_read_file(path):
        reads.append(Path(path))
        return gpd.GeoDataFrame()

    class FakeRaster:
        bounds = SimpleNamespace(left=1, bottom=2, right=3, top=4)
        crs = "EPSG:26911"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(main_mod.gpd, "read_file", fake_read_file)
    monkeypatch.setattr(main_mod.rasterio, "open", lambda path: FakeRaster())
    monkeypatch.setattr(main_mod, "transform_bounds", lambda *args: (10, 20, 30, 40))
    monkeypatch.setattr(main_mod, "render_folium_map", lambda *args: rendered.append(args))

    main_mod._render_map(out_dir, "dem.tif", 12)

    assert reads == [out_dir / "canyon_centerlines.gpkg", out_dir / "rappel_points.gpkg"]
    assert rendered[0][2:] == ((10, 20, 30, 40), str(out_dir / "canyon_map.html"), 12)
