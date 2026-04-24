from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import torch
from rasterio.windows import Window
from torch.utils.data import Dataset

from ..io import log, read_dem, write_raster
from .config import MLConfig
from .features import apply_norm, build_feature_stack
from .labels import build_labels_from_kml


# -----------------------------------------------------------------------------
# Manifest
# -----------------------------------------------------------------------------


@dataclass
class ManifestEntry:
    kml_path: str
    dem_path: str
    label_sigma_m: float | None = None
    rappel_sigma_m: float | None = None
    precision_tier: str | None = None  # "high" | "medium" | "low"; informational

    @property
    def dem_id(self) -> str:
        return hashlib.sha1(self.dem_path.encode()).hexdigest()[:10]


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON list of {kml_path, dem_path, ...} objects")
    return [ManifestEntry(**entry) for entry in data]


# -----------------------------------------------------------------------------
# Cache: per-DEM precomputed feature stack + canyon label + rappel heatmap.
# -----------------------------------------------------------------------------


def _cache_paths(cache_dir: Path, dem_id: str) -> dict[str, Path]:
    d = cache_dir / dem_id
    return {
        "dir": d,
        "features": d / "features.tif",
        "canyon": d / "canyon_label.tif",
        "rappel": d / "rappel_label.tif",
        "meta": d / "meta.json",
    }


def prepare_cache(manifest: list[ManifestEntry], cache_dir: str | Path, cfg: MLConfig) -> None:
    """Precompute feature stacks and label rasters for every manifest entry.

    Writes one subdirectory per DEM under `cache_dir` containing:
      - features.tif (6-band float32)
      - canyon_label.tif (float32, soft label)
      - rappel_label.tif (float32, heatmap)
      - meta.json (transform, crs, pixel_size, shape)
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    for entry in manifest:
        paths = _cache_paths(cache_dir, entry.dem_id)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        if paths["features"].exists() and paths["canyon"].exists():
            log(f"cache hit: {entry.dem_id} ({entry.dem_path})")
            continue

        log(f"preparing cache for {entry.dem_path}")
        elev, transform, crs, pixel_size = read_dem(entry.dem_path)
        stack = build_feature_stack(elev, transform, pixel_size, cfg)  # (C, H, W)
        canyon_y, rappel_y = build_labels_from_kml(
            entry.kml_path,
            transform,
            crs,
            elev.shape,
            pixel_size,
            cfg,
            label_sigma_m=entry.label_sigma_m,
            rappel_sigma_m=entry.rappel_sigma_m,
        )

        # Write features as a multi-band GeoTIFF (one band per channel).
        with rasterio.open(
            paths["features"], "w",
            driver="GTiff", height=elev.shape[0], width=elev.shape[1],
            count=stack.shape[0], dtype="float32",
            crs=crs, transform=transform,
            compress="deflate", predictor=2, tiled=True,
        ) as dst:
            for i in range(stack.shape[0]):
                dst.write(stack[i], i + 1)

        write_raster(paths["canyon"], canyon_y, transform, crs, dtype="float32")
        write_raster(paths["rappel"], rappel_y, transform, crs, dtype="float32")

        paths["meta"].write_text(
            json.dumps(
                {
                    "dem_path": entry.dem_path,
                    "kml_path": entry.kml_path,
                    "pixel_size": pixel_size,
                    "shape": list(elev.shape),
                    "crs": str(crs),
                    "label_sigma_m": entry.label_sigma_m,
                    "rappel_sigma_m": entry.rappel_sigma_m,
                    "precision_tier": entry.precision_tier,
                },
                indent=2,
            )
        )


# -----------------------------------------------------------------------------
# Tile index
# -----------------------------------------------------------------------------


@dataclass
class TileRecord:
    dem_id: str
    row: int
    col: int
    super_tile_id: str
    has_positive: bool


def _list_cached_dem_ids(cache_dir: Path) -> list[str]:
    return sorted(p.name for p in cache_dir.iterdir() if p.is_dir())


def build_tile_index(cache_dir: str | Path, cfg: MLConfig) -> list[TileRecord]:
    """Enumerate all tile origins per cached DEM with stride=train_stride.

    For each tile, inspect the canyon label max to set has_positive.
    """
    cache_dir = Path(cache_dir)
    records: list[TileRecord] = []
    tile = cfg.tile_size
    stride = cfg.train_stride
    super_px = cfg.super_tile_size

    for dem_id in _list_cached_dem_ids(cache_dir):
        paths = _cache_paths(cache_dir, dem_id)
        if not paths["canyon"].exists():
            continue
        with rasterio.open(paths["canyon"]) as src:
            h, w = src.height, src.width
            canyon_full = src.read(1)  # small enough to hold one at a time

        for r in range(0, h - tile + 1, stride):
            for c in range(0, w - tile + 1, stride):
                patch = canyon_full[r : r + tile, c : c + tile]
                has_pos = bool(patch.max() > cfg.positive_threshold)
                sid = f"{dem_id}:{r // super_px}:{c // super_px}"
                records.append(
                    TileRecord(
                        dem_id=dem_id, row=r, col=c, super_tile_id=sid, has_positive=has_pos
                    )
                )
    return records


# -----------------------------------------------------------------------------
# Spatial K-fold split (grouped by super-tile id)
# -----------------------------------------------------------------------------


class SpatialKFoldSplitter:
    def __init__(self, n_folds: int, seed: int = 0) -> None:
        self.n_folds = n_folds
        self.seed = seed

    def _fold_for(self, super_tile_id: str) -> int:
        h = hashlib.sha1(f"{self.seed}:{super_tile_id}".encode()).hexdigest()
        return int(h, 16) % self.n_folds

    def split(self, records: list[TileRecord]) -> list[tuple[list[int], list[int]]]:
        """Return [(train_idx, val_idx), ...] for each of n_folds."""
        assignments = [self._fold_for(r.super_tile_id) for r in records]
        folds: list[tuple[list[int], list[int]]] = []
        for k in range(self.n_folds):
            train = [i for i, f in enumerate(assignments) if f != k]
            val = [i for i, f in enumerate(assignments) if f == k]
            folds.append((train, val))
        return folds


# -----------------------------------------------------------------------------
# Torch Dataset
# -----------------------------------------------------------------------------


class TileDataset(Dataset):
    """Random-access dataset that reads a single (features, canyon, rappel) tile."""

    def __init__(
        self,
        cache_dir: str | Path,
        records: list[TileRecord],
        cfg: MLConfig,
        norm_stats: dict[str, list[float]],
        indices: list[int] | None = None,
        transform: Any | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.records = records
        self.cfg = cfg
        self.norm_stats = norm_stats
        self.indices = list(range(len(records))) if indices is None else list(indices)
        self.transform = transform
        self._src_cache: dict[str, dict[str, rasterio.io.DatasetReader]] = {}

    def __len__(self) -> int:
        return len(self.indices)

    def _sources(self, dem_id: str) -> dict[str, rasterio.io.DatasetReader]:
        # Lazy-open; each worker has its own instance (DataLoader forks).
        if dem_id not in self._src_cache:
            paths = _cache_paths(self.cache_dir, dem_id)
            self._src_cache[dem_id] = {
                "features": rasterio.open(paths["features"]),
                "canyon": rasterio.open(paths["canyon"]),
                "rappel": rasterio.open(paths["rappel"]),
            }
        return self._src_cache[dem_id]

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        rec = self.records[self.indices[idx]]
        tile = self.cfg.tile_size
        win = Window(rec.col, rec.row, tile, tile)
        src = self._sources(rec.dem_id)

        feat = src["features"].read(window=win).astype(np.float32)  # (C, H, W)
        canyon = src["canyon"].read(1, window=win).astype(np.float32)  # (H, W)
        rappel = src["rappel"].read(1, window=win).astype(np.float32)  # (H, W)

        feat = apply_norm(feat, self.norm_stats)

        if self.transform is not None:
            # albumentations expects HWC image; treat channels as "image" and targets as masks.
            out = self.transform(
                image=np.transpose(feat, (1, 2, 0)),
                canyon=canyon,
                rappel=rappel,
            )
            feat = np.transpose(out["image"], (2, 0, 1))
            canyon = out["canyon"]
            rappel = out["rappel"]

        return {
            "image": torch.from_numpy(np.ascontiguousarray(feat)),
            "canyon": torch.from_numpy(np.ascontiguousarray(canyon)).unsqueeze(0),
            "rappel": torch.from_numpy(np.ascontiguousarray(rappel)).unsqueeze(0),
            "has_positive": torch.tensor(rec.has_positive, dtype=torch.bool),
        }

    def close(self) -> None:
        for srcs in self._src_cache.values():
            for s in srcs.values():
                s.close()
        self._src_cache.clear()


def make_weighted_sampler(
    records: list[TileRecord], indices: list[int], cfg: MLConfig
) -> torch.utils.data.WeightedRandomSampler:
    """positive_tile_ratio fraction of draws should come from positive tiles."""
    pos_ratio = cfg.positive_tile_ratio
    weights = np.zeros(len(indices), dtype=np.float64)
    pos_count = sum(1 for i in indices if records[i].has_positive)
    neg_count = len(indices) - pos_count
    if pos_count == 0 or neg_count == 0:
        weights[:] = 1.0
    else:
        pos_w = pos_ratio / pos_count
        neg_w = (1.0 - pos_ratio) / neg_count
        for local, global_idx in enumerate(indices):
            weights[local] = pos_w if records[global_idx].has_positive else neg_w
    return torch.utils.data.WeightedRandomSampler(
        weights=weights.tolist(), num_samples=len(indices), replacement=True
    )
