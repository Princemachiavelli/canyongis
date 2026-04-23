from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from .config import Config
from .terrain import meters_to_px


def make_masks(
    slope: np.ndarray,
    tpi: np.ndarray,
    hand: np.ndarray,
    local_relief: np.ndarray,
    pixel_size: float,
    cfg: Config,
):
    """Return (channel_mask, channel_buffer, cliff_faces, rappel_heights)."""
    channel_mask = (hand < cfg.hand_threshold) & (slope > cfg.min_channel_slope)

    buf_r = meters_to_px(100, pixel_size)
    y, x = np.ogrid[-buf_r : buf_r + 1, -buf_r : buf_r + 1]
    channel_buffer = ndi.binary_dilation(
        channel_mask, structure=(x * x + y * y <= buf_r * buf_r)
    )

    cliff_faces = (
        (slope > cfg.cliff_slope_min) & (slope < cfg.cliff_slope_max) & channel_buffer
    )

    cliff_top = (
        (tpi > cfg.tpi_cliff_thresh)
        & (slope > cfg.cliff_slope_min - 5)
        & channel_buffer
    )
    rappel_mask = (
        cliff_top
        & (local_relief > cfg.min_drop_height)
        & (local_relief < cfg.max_drop_height)
    )
    rappel_heights = np.where(rappel_mask, local_relief, 0).astype(np.float32)

    return channel_mask, channel_buffer, cliff_faces, rappel_heights
