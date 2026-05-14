"""Unit tests for canyongis.web.assets — pure, no database."""

from __future__ import annotations

from pathlib import Path

from canyongis.web.assets import extract_asset_candidates


def _touch(path: Path, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_extract_candidates_picks_files_inside_output_root(tmp_path: Path) -> None:
    out = tmp_path / "outputs"
    dem = _touch(out / "areas" / "aoi" / "dem.tif")
    kml = _touch(out / "kml" / "abc" / "track.kml")
    external = _touch(tmp_path / "cache" / "elsewhere.tif")
    result = {
        "dem_path": str(dem),
        "kml_path": str(kml),
        "external_path": str(external),
        "bounds_wgs84": [1.0, 2.0, 3.0, 4.0],
    }
    cands = extract_asset_candidates(result, output_root=out)
    paths = {c.path for c in cands}
    assert str(dem.resolve()) in paths
    assert str(kml.resolve()) in paths
    assert str(external.resolve()) not in paths


def test_extract_candidates_dedupes_by_resolved_path(tmp_path: Path) -> None:
    out = tmp_path / "outputs"
    dem = _touch(out / "dem.tif")
    result = {"dem_path": str(dem), "hillshade_tif_path": str(dem)}
    cands = extract_asset_candidates(result, output_root=out)
    assert len(cands) == 1
    assert cands[0].path == str(dem.resolve())


def test_extract_candidates_classifies_by_extension(tmp_path: Path) -> None:
    out = tmp_path / "outputs"
    tif = _touch(out / "a.tif")
    kml = _touch(out / "b.kml")
    pmtiles = _touch(out / "c.pmtiles")
    gpkg = _touch(out / "d.gpkg")
    result = {
        "raster_path": str(tif),
        "kml_path": str(kml),
        "panel_pmtiles_path": str(pmtiles),
        "snapped_gpkg_path": str(gpkg),
    }
    by_kind = {c.kind: c for c in extract_asset_candidates(result, output_root=out)}
    assert by_kind["tif"].format == "tif"
    assert by_kind["kml"].media_type == "application/vnd.google-earth.kml+xml"
    assert by_kind["pmtiles"].format == "pmtiles"
    assert by_kind["gpkg"].format == "gpkg"


def test_extract_candidates_attaches_url_hint(tmp_path: Path) -> None:
    out = tmp_path / "outputs"
    tif = _touch(out / "a.tif")
    result = {
        "panel_b_tpi_path": str(tif),
        "panel_b_tpi_url": "/outputs/a.tif",
    }
    cands = extract_asset_candidates(result, output_root=out)
    assert cands[0].url == "/outputs/a.tif"


def test_extract_candidates_ignores_missing_files(tmp_path: Path) -> None:
    out = tmp_path / "outputs"
    out.mkdir()
    result = {"dem_path": str(out / "missing.tif")}
    assert extract_asset_candidates(result, output_root=out) == []
