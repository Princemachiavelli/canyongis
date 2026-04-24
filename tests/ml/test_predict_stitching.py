from __future__ import annotations

import numpy as np

from canyongis.ml.predict import _blend_window, _iter_tiles, _skeleton_to_paths


def test_blend_window_strictly_positive():
    w = _blend_window(32)
    assert w.shape == (32, 32)
    assert w.min() > 0
    # Center > edges.
    assert w[16, 16] > w[0, 0]


def test_iter_tiles_covers_edges():
    # DEM 300x300, tile 256, overlap 64 -> stride 192. Positions should cover to edge.
    positions = _iter_tiles(300, 300, tile=256, overlap=64)
    assert (0, 0) in positions
    # Last start row should be H - tile = 44 so that the tile extends to 300.
    rows = sorted({r for r, _ in positions})
    cols = sorted({c for _, c in positions})
    assert rows[-1] + 256 >= 300
    assert cols[-1] + 256 >= 300


def test_iter_tiles_small_raster():
    positions = _iter_tiles(50, 50, tile=256, overlap=64)
    assert positions == [(0, 0)]


def test_skeleton_to_paths_horizontal_line():
    skel = np.zeros((16, 16), dtype=bool)
    skel[8, 2:14] = True
    paths = _skeleton_to_paths(skel)
    # Exactly one path, length 12 pixels (start endpoint -> other endpoint).
    assert len(paths) == 1
    assert len(paths[0]) == 12


def test_skeleton_to_paths_empty():
    skel = np.zeros((8, 8), dtype=bool)
    assert _skeleton_to_paths(skel) == []


def test_skeleton_to_paths_branching_splits():
    # A Y-shape: vertical bar + two diagonals at the top.
    skel = np.zeros((16, 16), dtype=bool)
    skel[2:10, 8] = True  # vertical trunk
    skel[1, 7] = True  # left diagonal tip
    skel[1, 9] = True  # right diagonal tip
    paths = _skeleton_to_paths(skel)
    # Should produce at least 2 segments (branch point at (2, 8)).
    assert len(paths) >= 2
