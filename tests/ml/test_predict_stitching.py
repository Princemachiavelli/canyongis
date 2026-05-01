from __future__ import annotations

import numpy as np
import torch
from affine import Affine

from canyongis.ml.config import MLConfig
from canyongis.ml.predict import (
    _blend_window,
    _iter_tiles,
    _skeleton_to_paths,
    centerlines_to_gdf,
    predict_tiled,
)


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


def test_predict_tiled_batches_forward_calls_without_changing_output():
    class CountingModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.batch_sizes: list[int] = []

        def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
            self.calls += 1
            self.batch_sizes.append(x.shape[0])
            return {
                "canyon_logits": x[:, :1],
                "rappel_logits": -x[:, :1],
            }

    rng = np.random.default_rng(0)
    stack = rng.normal(size=(1, 96, 96)).astype(np.float32)
    norm_stats = {"mean": [0.0], "std": [1.0]}
    device = torch.device("cpu")
    cfg_single = MLConfig(tile_size=32, predict_overlap=16, predict_batch_size=1)
    cfg_batched = MLConfig(tile_size=32, predict_overlap=16, predict_batch_size=5)

    single_model = CountingModel()
    single = predict_tiled(single_model, stack, cfg_single, device, norm_stats)

    batched_model = CountingModel()
    batched = predict_tiled(batched_model, stack, cfg_batched, device, norm_stats)

    expected_tiles = len(_iter_tiles(96, 96, tile=32, overlap=16))
    assert single_model.calls == expected_tiles
    assert batched_model.calls == 5
    assert batched_model.batch_sizes == [5, 5, 5, 5, 5]
    assert np.allclose(batched[0], single[0])
    assert np.allclose(batched[1], single[1])


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


def test_centerlines_to_gdf_filters_short_segments():
    canyon_prob = np.zeros((16, 16), dtype=np.float32)
    canyon_prob[4, 2:5] = 0.9
    canyon_prob[10, 2:14] = 0.9

    gdf = centerlines_to_gdf(
        canyon_prob,
        Affine.translation(0, 0) * Affine.scale(1, -1),
        None,
        threshold=0.5,
        min_length_m=10.0,
    )

    assert len(gdf) == 1
    assert gdf.iloc[0].length_m >= 10.0
