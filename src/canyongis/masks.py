from __future__ import annotations

import numpy as np
from scipy import ndimage as ndi

from .config import Config
from .terrain import meters_to_px


def make_masks(
    slope: np.ndarray,
    tpi: np.ndarray,  # noqa: ARG001 — kept for API compat; no longer used
    hand: np.ndarray,
    local_relief: np.ndarray,  # noqa: ARG001 — kept for API compat; no longer used
    stream_mask: np.ndarray,
    elev: np.ndarray,
    pixel_size: float,
    cfg: Config,
):
    """Return (channel_mask, channel_buffer, cliff_faces, rappel_heights).

    Rappel detection model: each connected cliff-face cluster (steep cells near a
    stream) that *touches* the stream network and meets the size threshold becomes
    a single rappel point, placed at the highest-elevation stream pixel within
    the cluster. `drop_height = elev(top stream pixel) - elev(cluster min)` —
    the actual cliff-face relief.

    Earlier versions used a TPI-based cliff_top criterion, but in an incised
    canyon the rim has no TPI prominence and rappels sit below the walls, so
    that model pushed detections onto ridges far from the canyon.
    """
    channel_mask = (hand < cfg.hand_threshold) & (slope > cfg.min_channel_slope)

    buf_px = meters_to_px(cfg.cliff_buffer_m, pixel_size)
    channel_buffer = ndi.distance_transform_edt(~stream_mask, return_distances=True) <= buf_px

    cliff_faces = (slope > cfg.cliff_slope_min) & (slope < cfg.cliff_slope_max) & channel_buffer
    rappel_heights = np.zeros_like(slope, dtype=np.float32)

    # 8-connectivity so diagonal-touching cliff fragments merge into one cluster.
    labels, n_features = ndi.label(cliff_faces, structure=np.ones((3, 3), dtype=int))
    if n_features == 0:
        return channel_mask, channel_buffer, cliff_faces, rappel_heights

    # Keep only clusters that actually reach the stream — hillside cliffs are rejected.
    stream_labels = np.unique(labels[stream_mask])
    stream_labels = stream_labels[stream_labels > 0]
    if stream_labels.size == 0:
        return channel_mask, channel_buffer, cliff_faces, rappel_heights

    sizes = np.bincount(labels.ravel(), minlength=n_features + 1)
    valid_labels = stream_labels[sizes[stream_labels] >= cfg.min_cliff_face_cells]
    if valid_labels.size == 0:
        return channel_mask, channel_buffer, cliff_faces, rappel_heights

    # Top-of-drop = highest-elev STREAM pixel in the cluster. Drop = that elev
    # minus the cluster's overall minimum elevation (pool/channel below).
    elev_on_stream = np.where(stream_mask, elev, -np.inf).astype(np.float32, copy=False)
    min_elevs = np.asarray(ndi.minimum(elev, labels=labels, index=valid_labels), dtype=np.float32)
    top_elevs = np.asarray(
        ndi.maximum(elev_on_stream, labels=labels, index=valid_labels), dtype=np.float32
    )
    positions = ndi.maximum_position(elev_on_stream, labels=labels, index=valid_labels)
    drops = top_elevs - min_elevs

    for pos, drop in zip(positions, drops, strict=True):
        if not np.isfinite(drop):
            continue
        if cfg.min_drop_height <= drop <= cfg.max_drop_height:
            py, px = int(pos[0]), int(pos[1])
            rappel_heights[py, px] = drop

    return channel_mask, channel_buffer, cliff_faces, rappel_heights
