from __future__ import annotations

import gc
import json
from dataclasses import fields
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import torch
from rasterio.transform import xy as rio_xy
from shapely.geometry import LineString, Point
from skimage.feature import peak_local_max
from skimage.morphology import skeletonize

from ..io import log, read_dem, write_raster, write_vector
from .config import MLConfig
from .device import amp_enabled, get_device
from .features import apply_norm, build_feature_stack
from .model import build_model

# -----------------------------------------------------------------------------
# Tiled sliding-window inference with Gaussian-blended stitching
# -----------------------------------------------------------------------------


def _blend_window(tile_size: int) -> np.ndarray:
    """2D Hann * Gaussian window (tile_size, tile_size). Suppresses seams."""
    t = np.arange(tile_size, dtype=np.float32)
    hann = 0.5 - 0.5 * np.cos(2 * np.pi * t / (tile_size - 1))
    sigma = tile_size / 6.0
    g = np.exp(-0.5 * ((t - (tile_size - 1) / 2) / sigma) ** 2).astype(np.float32)
    w1d = hann * g
    w = np.outer(w1d, w1d).astype(np.float32)
    # Avoid zero weights at the corners — add a small epsilon.
    return w + 1e-6


def _iter_tiles(h: int, w: int, tile: int, overlap: int) -> list[tuple[int, int]]:
    stride = tile - overlap
    rows = list(range(0, max(h - tile, 0) + 1, stride))
    cols = list(range(0, max(w - tile, 0) + 1, stride))
    # Ensure the last row/col cover the edge.
    if rows and rows[-1] + tile < h:
        rows.append(h - tile)
    if cols and cols[-1] + tile < w:
        cols.append(w - tile)
    if not rows:
        rows = [0]
    if not cols:
        cols = [0]
    return [(r, c) for r in rows for c in cols]


def predict_tiled(
    model: torch.nn.Module,
    stack: np.ndarray,
    cfg: MLConfig,
    device: torch.device,
    norm_stats: dict[str, list[float]],
) -> tuple[np.ndarray, np.ndarray]:
    """Run sliding-window inference over a (C, H, W) feature stack.

    Returns (canyon_prob, rappel_prob), each (H, W) float32.
    """
    model.eval()
    use_amp = amp_enabled(device)
    _, h, w = stack.shape
    tile = cfg.tile_size
    overlap = cfg.predict_overlap

    # Pad if DEM is smaller than tile.
    pad_h = max(tile - h, 0)
    pad_w = max(tile - w, 0)
    if pad_h or pad_w:
        stack = np.pad(stack, ((0, 0), (0, pad_h), (0, pad_w)), mode="edge")
    _, H, W = stack.shape

    canyon_acc = np.zeros((H, W), dtype=np.float32)
    rappel_acc = np.zeros((H, W), dtype=np.float32)
    weight_acc = np.zeros((H, W), dtype=np.float32)
    window = _blend_window(tile)

    positions = _iter_tiles(H, W, tile, overlap)
    batch_size = max(1, int(cfg.predict_batch_size))
    for i in range(0, len(positions), batch_size):
        batch_positions = positions[i : i + batch_size]
        batch_patches = [
            apply_norm(stack[:, r : r + tile, col : col + tile], norm_stats)
            for r, col in batch_positions
        ]
        x = torch.from_numpy(np.ascontiguousarray(np.stack(batch_patches, axis=0))).to(device)
        with torch.no_grad(), torch.autocast(device_type=device.type, enabled=use_amp):
            out = model(x)
            probs = torch.sigmoid(torch.cat((out["canyon_logits"], out["rappel_logits"]), dim=1))
            probs = probs.float().cpu().numpy()
            p_c_batch = probs[:, 0]
            p_r_batch = probs[:, 1]
        for (r, col), p_c, p_r in zip(batch_positions, p_c_batch, p_r_batch, strict=True):
            canyon_acc[r : r + tile, col : col + tile] += p_c * window
            rappel_acc[r : r + tile, col : col + tile] += p_r * window
            weight_acc[r : r + tile, col : col + tile] += window
        if i % 16 == 0:
            log(f"  tile {i + 1}/{len(positions)}")

    canyon = canyon_acc / np.maximum(weight_acc, 1e-6)
    rappel = rappel_acc / np.maximum(weight_acc, 1e-6)
    if pad_h or pad_w:
        canyon = canyon[:h, :w]
        rappel = rappel[:h, :w]
    return canyon.astype(np.float32), rappel.astype(np.float32)


# -----------------------------------------------------------------------------
# Vectorization: skeleton -> polylines; heatmap peaks -> points
# -----------------------------------------------------------------------------


def _skeleton_to_paths(skel: np.ndarray) -> list[list[tuple[int, int]]]:
    """Convert a binary skeleton to pixel paths. Branches split into separate paths."""
    ys, xs = np.where(skel)
    pixels = set(zip(ys.tolist(), xs.tolist(), strict=True))
    if not pixels:
        return []

    def nbrs(p: tuple[int, int]) -> list[tuple[int, int]]:
        y, x = p
        out = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                q = (y + dy, x + dx)
                if q in pixels:
                    out.append(q)
        return out

    # Walk until we hit a branch or endpoint; split there.
    visited_edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def edge_key(a, b):
        return (a, b) if a < b else (b, a)

    def walk(start: tuple[int, int]) -> list[list[tuple[int, int]]]:
        results: list[list[tuple[int, int]]] = []
        for first in nbrs(start):
            if edge_key(start, first) in visited_edges:
                continue
            path = [start, first]
            visited_edges.add(edge_key(start, first))
            cur, prev = first, start
            while True:
                ns = [n for n in nbrs(cur) if n != prev and edge_key(cur, n) not in visited_edges]
                if len(ns) != 1 or len(nbrs(cur)) > 2:
                    break
                nxt = ns[0]
                visited_edges.add(edge_key(cur, nxt))
                path.append(nxt)
                prev, cur = cur, nxt
            if len(path) >= 2:
                results.append(path)
        return results

    paths: list[list[tuple[int, int]]] = []
    # Prefer starting at endpoints and branch points.
    starts = [p for p in pixels if len(nbrs(p)) == 1 or len(nbrs(p)) >= 3]
    for start in starts:
        paths.extend(walk(start))
    # Any isolated loops
    for p in pixels:
        if all(edge_key(p, n) in visited_edges for n in nbrs(p)):
            continue
        paths.extend(walk(p))
    return paths


def centerlines_to_gdf(
    canyon_prob: np.ndarray,
    transform,
    crs,
    threshold: float,
    min_length_m: float = 0.0,
) -> gpd.GeoDataFrame:
    binary = canyon_prob >= threshold
    if not binary.any():
        return gpd.GeoDataFrame(
            columns=["mean_prob", "length_m", "geometry"], geometry="geometry", crs=crs
        )
    skel = skeletonize(binary)
    paths = _skeleton_to_paths(skel)
    rows = []
    for path in paths:
        if len(path) < 2:
            continue
        coords = [rio_xy(transform, y, x) for (y, x) in path]
        probs = [float(canyon_prob[y, x]) for (y, x) in path]
        line = LineString(coords)
        length_m = float(line.length)
        if length_m < min_length_m:
            continue
        rows.append({"mean_prob": float(np.mean(probs)), "length_m": length_m, "geometry": line})
    if not rows:
        return gpd.GeoDataFrame(
            columns=["mean_prob", "length_m", "geometry"], geometry="geometry", crs=crs
        )
    return gpd.GeoDataFrame(rows, crs=crs)


def rappel_peaks_to_gdf(
    rappel_prob: np.ndarray,
    transform,
    crs,
    pixel_size: float,
    cfg: MLConfig,
) -> gpd.GeoDataFrame:
    min_distance = max(1, round(cfg.rappel_peak_min_distance_m / max(pixel_size, 1e-6)))
    peaks = peak_local_max(rappel_prob, min_distance=min_distance, threshold_abs=cfg.prob_threshold)
    if peaks.size == 0:
        return gpd.GeoDataFrame(columns=["prob", "geometry"], geometry="geometry", crs=crs)
    rows = []
    for y, x in peaks:
        lon, lat = rio_xy(transform, int(y), int(x))
        rows.append({"prob": float(rappel_prob[y, x]), "geometry": Point(lon, lat)})
    return gpd.GeoDataFrame(rows, crs=crs)


# -----------------------------------------------------------------------------
# Top-level CLI-facing entry
# -----------------------------------------------------------------------------


def _apply_checkpoint_defaults(cfg: MLConfig, saved_cfg: dict[str, Any]) -> None:
    """Use training config as prediction defaults, preserving explicit CLI overrides."""
    if not getattr(cfg, "_use_checkpoint_defaults", False):
        return

    skip = {"manifest_path", "out_dir", "dem_path", "checkpoint_path"}
    explicit = set(getattr(cfg, "_cli_overrides", set()))
    valid_keys = {f.name for f in fields(MLConfig())}
    for key, value in saved_cfg.items():
        if key in skip or key in explicit or key not in valid_keys:
            continue
        if key == "decoder_channels" and isinstance(value, list):
            value = tuple(value)
        setattr(cfg, key, value)


def run_inference(cfg: MLConfig) -> dict[str, Any]:
    """Read DEM → features → model → rasters → vectors. Writes outputs under cfg.out_dir."""
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"loading checkpoint: {cfg.checkpoint_path}")
    ckpt = torch.load(cfg.checkpoint_path, map_location="cpu", weights_only=False)
    # Prefer checkpoint's saved cfg dict for architecture fields (encoder, channels).
    saved_cfg_dict = ckpt.get("cfg", {})
    _apply_checkpoint_defaults(cfg, saved_cfg_dict)
    arch_keys = ("encoder_name", "in_channels", "decoder_channels", "encoder_weights")
    for k in arch_keys:
        if k in saved_cfg_dict:
            setattr(cfg, k, saved_cfg_dict[k])

    device = get_device()
    log(f"device={device} amp={amp_enabled(device)}")

    model = build_model(cfg).to(device)
    model.load_state_dict(ckpt["model_state"])
    model.eval()

    norm_stats = ckpt.get("norm_stats")
    if norm_stats is None:
        norm_path = Path(cfg.checkpoint_path).parent / "norm.json"
        norm_stats = json.loads(norm_path.read_text())

    log(f"reading DEM: {cfg.dem_path}")
    elev, transform, crs, pixel_size = read_dem(cfg.dem_path)

    log("building feature stack")
    stack = build_feature_stack(elev, transform, pixel_size, cfg, dem_path=cfg.dem_path)

    log("running tiled inference")
    canyon_prob, rappel_prob = predict_tiled(model, stack, cfg, device, norm_stats)

    del model, stack
    if device.type == "cuda":
        torch.cuda.empty_cache()
    gc.collect()

    write_raster(out_dir / "canyon_prob.tif", canyon_prob, transform, crs, dtype="float32")
    write_raster(out_dir / "rappel_prob.tif", rappel_prob, transform, crs, dtype="float32")

    log("vectorizing centerlines and rappel peaks")
    centerlines = centerlines_to_gdf(
        canyon_prob,
        transform,
        crs,
        cfg.prob_threshold,
        min_length_m=cfg.min_centerline_length_m,
    )
    rappels = rappel_peaks_to_gdf(rappel_prob, transform, crs, pixel_size, cfg)
    write_vector(centerlines, out_dir / "canyon_centerlines")
    write_vector(rappels, out_dir / "rappel_points")
    log(f"wrote {len(centerlines)} centerlines, {len(rappels)} rappel points")

    return {"centerlines": len(centerlines), "rappels": len(rappels)}
