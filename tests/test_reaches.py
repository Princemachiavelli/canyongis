from __future__ import annotations

import numpy as np
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

from canyongis.io import write_raster
from canyongis.reach_eval import area_matched_labels, rank_metrics
from canyongis.reaches import (
    MosaicReaches,
    ReachConfig,
    build_reaches,
    label_reaches,
    plan_tiles,
)

X0, Y0 = 500_000.0, 4_000_000.0
EPSG = 32612
CHANNEL_COL = 80
KNICK_ROW = 120
H, W = 220, 160

CFG = ReachConfig(
    min_area_m2=2_000.0,
    reach_length_m=20.0,
    depth_radii_m=(20.0, 40.0),
    xs_max_m=30.0,
    xs_wall_m=(10.0, 25.0),
    drop_window_m=6.0,
    grade_window_m=30.0,
    label_tol_m=8.0,
)


def _canyon_dem(tmp_path):
    """South-sloping valley with a 15 m V-incision along x=80 and a 6 m step at row 120."""
    yy, xx = np.mgrid[:H, :W].astype(np.float32)
    elev = 300.0 - 0.2 * yy + 0.3 * np.abs(xx - CHANNEL_COL)
    elev -= np.clip(15.0 - 2.5 * np.abs(xx - CHANNEL_COL), 0.0, None)
    elev -= 6.0 * (yy > KNICK_ROW)
    path = tmp_path / "dem.tif"
    write_raster(
        path,
        elev.astype(np.float32),
        from_origin(X0, Y0, 1.0, 1.0),
        rasterio.crs.CRS.from_epsg(EPSG),
    )
    return path


def _descent_kml(tmp_path, rows=(20, 200)):
    tf = Transformer.from_crs(EPSG, 4326, always_xy=True)
    pts = [tf.transform(X0 + CHANNEL_COL + 0.5, Y0 - r - 0.5) for r in rows]
    coords = " ".join(f"{lon},{lat},0" for lon, lat in pts)
    path = tmp_path / "route.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Descent</name><LineString><coordinates>{coords}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )
    return path


@pytest.fixture
def built(tmp_path):
    return tmp_path, build_reaches(_canyon_dem(tmp_path), CFG)


def test_network_and_features(built):
    _, mr = built
    assert mr.n >= 3
    assert len(mr.reach_ptr) == mr.n + 1
    assert int(np.diff(mr.reach_ptr).sum()) == len(mr.pix_r)
    # Downstream links form a forest: every pointer is -1 or a valid reach id.
    assert ((mr.down == -1) | ((mr.down >= 0) & (mr.down < mr.n))).all()

    on_channel = np.array(
        [abs(np.median(mr.reach_pixels(i)[1]) - CHANNEL_COL) <= 2 for i in range(mr.n)]
    )
    assert on_channel.any()
    # The incised channel is deeper and more walled-in than everything else.
    assert mr.features["depth40"][on_channel].mean() > mr.features["depth40"][~on_channel].mean()
    assert np.nanmedian(mr.features["wall10"][on_channel]) > 5.0
    for name in ("log_area", "gradient", "max_drop", "width5", "ctx_depth150"):
        if name in mr.features:
            assert mr.features[name].shape == (mr.n,)

    # The 6 m knickpoint yields a profile-step candidate near its row.
    near_knick = (np.abs(mr.drop_r - KNICK_ROW) <= 8) & (np.abs(mr.drop_c - CHANNEL_COL) <= 2)
    assert near_knick.any()
    assert mr.drop_m[near_knick].max() >= 4.0


def test_labels_and_roundtrip(built):
    tmp_path, mr = built
    label_reaches(mr, [_descent_kml(tmp_path)], CFG)
    pos = mr.label == 1
    assert pos.any()
    for i in np.flatnonzero(pos):
        assert abs(np.median(mr.reach_pixels(i)[1]) - CHANNEL_COL) <= 3
    assert (mr.route_frac[pos] >= CFG.label_min_frac).all()

    path = tmp_path / "cache" / "r.npz"
    mr.save(path)
    back = MosaicReaches.load(path)
    assert back.n == mr.n
    np.testing.assert_array_equal(back.label, mr.label)
    np.testing.assert_allclose(back.features["depth40"], mr.features["depth40"])
    assert len(back.to_gdf()) == mr.n


def test_signature_ignores_label_fields():
    assert CFG.signature() == ReachConfig(**{**CFG.__dict__, "label_tol_m": 99.0}).signature()
    assert CFG.signature() != ReachConfig(**{**CFG.__dict__, "min_area_m2": 99.0}).signature()


def test_plan_tiles_cores_partition_raster():
    tiles = plan_tiles(250, 170, tile_px=100, overlap_px=20)
    cover = np.zeros((250, 170), dtype=int)
    for t in tiles:
        r0, r1 = t.row_off + t.core_rows[0], t.row_off + t.core_rows[1]
        c0, c1 = t.col_off + t.core_cols[0], t.col_off + t.core_cols[1]
        cover[r0:r1, c0:c1] += 1
        assert t.height <= 140 and t.width <= 140
    assert (cover == 1).all()


def test_rank_metrics_and_area_matching():
    label = np.array([1, 1, 0, 0, 0, -1], dtype=np.int8)
    w = np.ones(6)
    good = rank_metrics(np.array([0.9, 0.8, 0.3, 0.2, 0.1, 0.99]), label, w)
    assert good["auc"] == pytest.approx(1.0)
    assert good["ap"] == pytest.approx(1.0)
    bad = rank_metrics(np.array([0.1, 0.2, 0.7, 0.8, 0.9, 0.0]), label, w)
    assert bad["auc"] == pytest.approx(0.0)

    class _MR:
        def __init__(self, la, lab):
            self.features = {"log_area": np.asarray(la, dtype=np.float32)}
            self.label = np.asarray(lab, dtype=np.int8)
            self.length_m = np.ones(len(lab), dtype=np.float32)

    matched, thr = area_matched_labels([_MR([5.0, 6.0, 3.0, 5.5], [1, 1, 0, 0])])
    assert thr == pytest.approx(5.0)
    # The tiny negative drops out; the large one stays as a hard negative.
    np.testing.assert_array_equal(matched[0], [1, 1, -1, 0])


def test_canyon_balanced_weights_equalize_per_canyon_totals(built):
    from canyongis.reach_eval import _canyon_balanced_weights

    tmp_path, mr = built
    label_reaches(mr, [_descent_kml(tmp_path)], CFG)
    pos = mr.label == 1
    assert (mr.canyon[pos] == 0).all()
    assert (mr.canyon[~pos] == -1).all()

    # A single canyon's total already equals the mean canyon total, so balancing
    # leaves its weights unchanged; negatives always keep their length weights.
    (w,) = _canyon_balanced_weights([mr])
    np.testing.assert_allclose(w[pos].sum(), mr.length_m[pos].sum(), rtol=1e-6)
    np.testing.assert_allclose(w[~pos], mr.length_m[~pos])


def _kml_with_points(tmp_path, points: dict[str, int]):
    """Descent down the channel (rows 20→200) plus named points at given rows."""
    tf = Transformer.from_crs(EPSG, 4326, always_xy=True)

    def lonlat(r):
        return tf.transform(X0 + CHANNEL_COL + 0.5, Y0 - r - 0.5)

    line = " ".join(f"{lon},{lat},0" for lon, lat in (lonlat(20), lonlat(200)))
    marks = "".join(
        f"<Placemark><name>{name}</name><Point><coordinates>{lonlat(r)[0]},{lonlat(r)[1]},0"
        "</coordinates></Point></Placemark>"
        for name, r in points.items()
    )
    path = tmp_path / "marked.kml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Descent</name><LineString><coordinates>{line}"
        f"</coordinates></LineString></Placemark>{marks}</Document></kml>"
    )
    return path


def test_technical_section_spans_named_rappels(tmp_path):
    from canyongis.reach_eval import technical_section

    kml = _kml_with_points(tmp_path, {"Drop in": 25, "R1 10 ft": 80, "R2": 130, "Exit": 195})
    sec = technical_section(kml, ropewiki_rappels=2.0, margin_m=10.0)
    assert sec.n_anchors == 2  # the drop-in and exit aren't rappels
    assert sec.lines is not None
    assert sec.full_km == pytest.approx(0.180, abs=0.002)
    assert sec.section_km == pytest.approx(0.070, abs=0.002)  # rows 70..140

    # A lone R1 on a multi-rappel canyon doesn't locate the technical part.
    lone = _kml_with_points(tmp_path, {"R1": 80})
    assert technical_section(lone, ropewiki_rappels=3.0).lines is None
    assert technical_section(lone, ropewiki_rappels=1.0).lines is not None


def test_technical_section_uses_unclassified_track_with_rappels(tmp_path):
    from canyongis.reach_eval import technical_section

    kml = _kml_with_points(tmp_path, {"R1": 80, "R2": 130})
    kml.write_text(kml.read_text().replace("<name>Descent</name>", "<name>Track</name>"))
    sec = technical_section(kml, ropewiki_rappels=2.0, margin_m=10.0)
    assert sec.lines is not None
    assert sec.section_km == pytest.approx(0.070, abs=0.002)


def test_technical_section_survives_self_crossing_track(tmp_path):
    from canyongis.reach_eval import technical_section

    # R1 sits before a loop where the track crosses itself, R2 after it. The section
    # must follow the whole track between them (union_all() would node the track at
    # the crossing and leave each rappel on its own fragment).
    kml = _kml_with_points(tmp_path, {"R1": 30, "R2": 130})
    tf = Transformer.from_crs(EPSG, 4326, always_xy=True)
    c = CHANNEL_COL
    xy = [(c, 20), (c, 60), (c + 10, 50), (c - 10, 50), (c, 65), (c, 200)]
    coords = " ".join(
        "{},{},0".format(*tf.transform(X0 + col + 0.5, Y0 - r - 0.5)) for col, r in xy
    )
    text = kml.read_text()
    start = text.index("<LineString><coordinates>") + len("<LineString><coordinates>")
    end = text.index("</coordinates></LineString>")
    kml.write_text(text[:start] + coords + text[end:])
    sec = technical_section(kml, ropewiki_rappels=2.0, margin_m=10.0)
    # R1 → R2 along the track: 30 + 14.1 + 20 + 18.0 + 65 = 147.2 m, plus 2 x 10 m.
    assert sec.section_km == pytest.approx(0.167, abs=0.003)


def test_smooth_scores_stays_on_main_stem():
    from canyongis.reaches import smooth_scores

    # Trunk 0 → 1 → 2 → 3; tributary 4 joins at 2 (2's main stem continues up to 1).
    down = np.array([1, 2, 3, -1, 2])
    main_up = np.array([-1, 0, 1, 2, -1])
    length = np.ones(5)
    score = np.array([0.9, 0.9, 0.9, 0.9, 0.1])
    sm = smooth_scores(score, length, down, main_up, hops=2, how="median")
    # The gully keeps its own score instead of inheriting the canyon's ...
    assert sm[4] == pytest.approx(0.1)
    # ... and the canyon isn't dragged down by the gully.
    np.testing.assert_allclose(sm[:4], 0.9)
    mean = smooth_scores(np.array([1.0, 0.0, 1.0, 1.0, 0.0]), length, down, main_up, 1, "mean")
    assert mean[1] == pytest.approx(2.0 / 3.0)


def test_isotonic_steps_ignore_pits_and_find_drops():
    from canyongis.reaches import ProfileConfig, isotonic_steps

    dist = np.arange(200, dtype=np.float64)
    z = 100.0 - 0.05 * dist
    z[60:63] -= 2.5  # a pool / DEM pit: not a step
    z[120:] -= 6.0  # a 6 m vertical drop
    pcfg = ProfileConfig(step_window_m=3.0, step_grade=0.7, min_step_m=3.0)
    steep, steps = isotonic_steps(z, dist, len(z), pcfg)
    assert len(steps) == 1
    top, height = steps[0]
    assert 115 <= top <= 120
    assert height == pytest.approx(6.0, abs=0.6)
    assert not steep[55:70].any()


def test_snapped_labels_fill_gaps_and_tolerate_offsets(built):
    tmp_path, mr = built
    tf = Transformer.from_crs(EPSG, 4326, always_xy=True)
    # Track 12 m east of the channel (beyond label_tol_m = 8), swinging 45 m out
    # (beyond the snap distance) between rows 80 and 140.
    xy = [(CHANNEL_COL + 12, 20), (CHANNEL_COL + 12, 75), (CHANNEL_COL + 45, 85)]
    xy += [(CHANNEL_COL + 45, 135), (CHANNEL_COL + 12, 145), (CHANNEL_COL + 12, 200)]
    coords = " ".join(
        "{},{},0".format(*tf.transform(X0 + col + 0.5, Y0 - r - 0.5)) for col, r in xy
    )
    kml = tmp_path / "offset.kml"
    kml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Descent</name><LineString><coordinates>{coords}"
        "</coordinates></LineString></Placemark></Document></kml>"
    )
    on_channel = np.array(
        [abs(np.median(mr.reach_pixels(i)[1]) - CHANNEL_COL) <= 2 for i in range(mr.n)]
    )
    mid_rows = np.array([np.median(mr.reach_pixels(i)[0]) for i in range(mr.n)])
    gap = on_channel & (mid_rows > 95) & (mid_rows < 125)
    assert gap.any()

    label_reaches(mr, [kml], CFG)
    pixel_pos = mr.label == 1
    snap = ReachConfig(**{**CFG.__dict__, "label_snap_m": 20.0})
    label_reaches(mr, [kml], snap)
    snap_pos = mr.label == 1
    assert snap_pos.sum() > pixel_pos.sum()
    assert (snap_pos[gap]).all()  # filled along the network, not matched by distance
    assert not (snap_pos & ~on_channel).any()
    (st,) = mr.snap_stats
    assert 0.5 < st["matched_frac"] < 1.0
    assert st["median_offset_m"] == pytest.approx(12.0, abs=2.0)
    assert snap.signature() == CFG.signature()
