import numpy as np

from canyongis.ravine_spine import (
    _build_skeleton_adj,
    _elevation_profile,
    _orient_path_downhill,
    _path_length_m,
    _path_rappel_candidates,
    extract_trunk_and_branches,
)


def test_skeleton_adjacency_does_not_cut_orthogonal_corners():
    skel = np.zeros((2, 2), dtype=bool)
    skel[0, 0] = True
    skel[0, 1] = True
    skel[1, 1] = True

    adj = _build_skeleton_adj(skel, pixel_size=1.0)

    assert (1, 1) not in {node for node, _length in adj[(0, 0)]}
    assert (0, 0) not in {node for node, _length in adj[(1, 1)]}


def test_trunk_path_follows_corner_pixels_instead_of_diagonal_shortcut():
    skel = np.zeros((2, 2), dtype=bool)
    skel[0, 0] = True
    skel[0, 1] = True
    skel[1, 1] = True

    paths = extract_trunk_and_branches(skel, pixel_size=1.0, branch_keep_ratio=0)

    assert len(paths) == 1
    assert set(paths[0]) == {(0, 0), (0, 1), (1, 1)}
    assert _path_length_m(paths[0], pixel_size=1.0) == 2.0


def test_elevation_profile_flags_spur_crossing_as_uphill_gain():
    elev = np.array([[100.0, 95.0, 110.0, 90.0, 80.0]], dtype=np.float32)
    path = [(0, x) for x in range(elev.shape[1])]

    profile = _elevation_profile(path, elev, pixel_size=1.0)

    assert profile.net_drop_m == 20.0
    assert profile.total_drop_m == 35.0
    assert profile.total_gain_m == 15.0
    assert profile.max_step_gain_m == 15.0
    assert profile.reversal_count == 2
    assert profile.uphill_fraction == 0.25


def test_orient_path_downhill_reverses_uphill_paths():
    elev = np.array([[80.0, 90.0, 100.0]], dtype=np.float32)
    path = [(0, 0), (0, 1), (0, 2)]

    oriented = _orient_path_downhill(path, elev, pixel_size=1.0)
    profile = _elevation_profile(oriented, elev, pixel_size=1.0)

    assert oriented == [(0, 2), (0, 1), (0, 0)]
    assert profile.net_drop_m == 20.0
    assert profile.total_gain_m == 0.0


def test_rappel_candidates_find_sudden_drop_window():
    elev = np.array([[100.0, 99.0, 98.0, 88.0, 87.5, 87.0]], dtype=np.float32)
    path = [(0, x) for x in range(elev.shape[1])]

    candidates = _path_rappel_candidates(
        1,
        path,
        elev,
        pixel_size=1.0,
        window_m=2.0,
        min_drop_m=8.0,
        min_grade=4.0,
        min_spacing_m=2.0,
    )

    assert len(candidates) == 1
    assert candidates[0]["path_id"] == 1
    assert candidates[0]["pixel"] == (0, 2)
    assert candidates[0]["drop_m"] == 10.0
