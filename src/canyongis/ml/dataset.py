from __future__ import annotations

import hashlib
import json
import math
import multiprocessing as mp
import os
import shutil
import tempfile
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import torch
from rasterio.windows import Window
from torch.utils.data import Dataset

from ..io import log, read_dem
from .config import MLConfig
from .features import apply_norm, build_feature_stack, feature_stack_signature
from .labels import build_labels_from_kml_paths

# -----------------------------------------------------------------------------
# Manifest
# -----------------------------------------------------------------------------


@dataclass(init=False)
class ManifestEntry:
    dem_path: str
    kml_paths: tuple[str, ...]
    label_sigma_m: float | None = None
    rappel_sigma_m: float | None = None
    precision_tier: str | None = None  # "high" | "medium" | "low"; informational

    def __init__(
        self,
        *,
        dem_path: str,
        kml_path: str | None = None,
        kml_paths: list[str] | tuple[str, ...] | None = None,
        label_sigma_m: float | None = None,
        rappel_sigma_m: float | None = None,
        precision_tier: str | None = None,
    ) -> None:
        if kml_paths is None:
            if kml_path is None:
                raise ValueError("manifest entry must include kml_path or kml_paths")
            paths = (kml_path,)
        else:
            paths = tuple(str(path) for path in kml_paths)
            if kml_path is not None:
                paths = (kml_path, *paths)
        paths = tuple(dict.fromkeys(paths))
        if not paths:
            raise ValueError("manifest entry kml_paths must not be empty")

        self.dem_path = dem_path
        self.kml_paths = paths
        self.label_sigma_m = label_sigma_m
        self.rappel_sigma_m = rappel_sigma_m
        self.precision_tier = precision_tier

    @property
    def kml_path(self) -> str:
        return self.kml_paths[0]

    @property
    def dem_id(self) -> str:
        return hashlib.sha1(self.dem_path.encode()).hexdigest()[:10]


def load_manifest(path: str | Path) -> list[ManifestEntry]:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON list of {kml_paths, dem_path, ...} objects")
    entries = [ManifestEntry(**entry) for entry in data]
    by_dem: dict[str, ManifestEntry] = {}
    for entry in entries:
        if entry.dem_path not in by_dem:
            by_dem[entry.dem_path] = entry
            continue
        existing = by_dem[entry.dem_path]
        existing.kml_paths = tuple(dict.fromkeys((*existing.kml_paths, *entry.kml_paths)))
    return list(by_dem.values())


# -----------------------------------------------------------------------------
# Cache: per-DEM precomputed feature stack + canyon label + rappel heatmap.
# -----------------------------------------------------------------------------


def _cache_paths(cache_dir: Path, dem_id: str) -> dict[str, Path]:
    d = cache_dir / dem_id
    return {
        "dir": d,
        "zarr": d / "cache.zarr",
        "features": d / "features.tif",
        "canyon": d / "canyon_label.tif",
        "rappel": d / "rappel_label.tif",
        "meta": d / "meta.json",
    }


def _cache_format(cfg: MLConfig) -> str:
    cache_format = cfg.cache_format.strip().lower()
    if cache_format not in {"zarr", "geotiff"}:
        raise ValueError(f"unsupported cache_format={cfg.cache_format!r}; use zarr or geotiff")
    return cache_format


def _cache_feature_count(paths: dict[str, Path], cfg: MLConfig) -> int | None:
    if _cache_format(cfg) == "zarr":
        if not paths["zarr"].exists():
            return None
        import zarr

        root = zarr.open_group(paths["zarr"], mode="r")
        if "features" not in root:
            return None
        return int(root["features"].shape[0])

    if not paths["features"].exists() or not paths["canyon"].exists():
        return None
    with rasterio.open(paths["features"]) as src:
        return src.count


def _cache_feature_signature(paths: dict[str, Path]) -> dict[str, Any] | None:
    if not paths["meta"].exists():
        return None
    try:
        meta = json.loads(paths["meta"].read_text())
    except json.JSONDecodeError:
        return None
    signature = meta.get("feature_stack")
    return signature if isinstance(signature, dict) else None


def _cache_storage_signature(cfg: MLConfig) -> dict[str, Any]:
    cache_format = _cache_format(cfg)
    compress = cfg.cache_compress.strip().lower()
    if cache_format == "zarr":
        block_size = _resolved_cache_block_size(cfg)
        return {
            "format": "zarr",
            "compress": compress,
            "feature_chunks": [cfg.in_channels, block_size, block_size],
            "label_chunks": [block_size, block_size],
        }
    return {
        "format": "geotiff",
        "compress": compress,
        "block_size": _resolved_cache_block_size(cfg),
    }


def _resolved_cache_block_size(cfg: MLConfig) -> int:
    explicit = int(cfg.cache_block_size)
    if explicit > 0:
        if explicit < 16 or explicit % 16 != 0:
            raise ValueError("cache_block_size must be 0 or a multiple of 16 >= 16")
        return explicit
    else:
        block_size = math.gcd(max(1, int(cfg.tile_size)), max(1, int(cfg.train_stride)))

    block_size = max(16, block_size)
    if block_size % 16 != 0:
        block_size = max(16, (block_size // 16) * 16)
    return block_size


def _cache_storage_options(cfg: MLConfig) -> dict[str, Any]:
    block_size = _resolved_cache_block_size(cfg)
    compress = cfg.cache_compress.strip().lower()
    options: dict[str, Any] = {
        "tiled": True,
        "blockxsize": block_size,
        "blockysize": block_size,
    }
    if compress in {"", "none", "no", "false"}:
        return options
    if compress == "deflate":
        options.update(compress="deflate", predictor=3)
        return options
    if compress == "zstd":
        options.update(compress="zstd", predictor=3, zstd_level=1)
        return options
    raise ValueError(
        f"unsupported cache_compress={cfg.cache_compress!r}; use none, deflate, or zstd"
    )


def _zarr_compressors(cfg: MLConfig):
    compress = cfg.cache_compress.strip().lower()
    if compress in {"", "none", "no", "false"}:
        return None
    raise ValueError(
        f"unsupported zarr cache_compress={cfg.cache_compress!r}; use none for zarr cache"
    )


def _cache_storage_matches(paths: dict[str, Path], cfg: MLConfig) -> bool:
    if not paths["meta"].exists():
        return False
    try:
        meta = json.loads(paths["meta"].read_text())
    except json.JSONDecodeError:
        return False
    return meta.get("cache_storage") == _cache_storage_signature(cfg)


def _write_cache_raster(
    path: Path,
    arr: np.ndarray,
    transform,
    crs,
    cfg: MLConfig,
    *,
    dtype: str = "float32",
) -> None:
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=arr.shape[0],
        width=arr.shape[1],
        count=1,
        dtype=dtype,
        crs=crs,
        transform=transform,
        **_cache_storage_options(cfg),
    ) as dst:
        dst.write(arr.astype(dtype, copy=False), 1)


def _write_zarr_cache(
    paths: dict[str, Path],
    stack: np.ndarray,
    canyon_y: np.ndarray,
    rappel_y: np.ndarray,
    cfg: MLConfig,
) -> None:
    import zarr

    if paths["zarr"].exists():
        shutil.rmtree(paths["zarr"])

    block_size = _resolved_cache_block_size(cfg)
    compressors = _zarr_compressors(cfg)
    root = zarr.open_group(paths["zarr"], mode="w")
    root.create_array(
        "features",
        data=stack.astype(np.float32, copy=False),
        chunks=(stack.shape[0], block_size, block_size),
        compressors=compressors,
        overwrite=True,
    )
    root.create_array(
        "canyon",
        data=canyon_y.astype(np.float32, copy=False),
        chunks=(block_size, block_size),
        compressors=compressors,
        overwrite=True,
    )
    root.create_array(
        "rappel",
        data=rappel_y.astype(np.float32, copy=False),
        chunks=(block_size, block_size),
        compressors=compressors,
        overwrite=True,
    )


def _write_geotiff_cache(
    paths: dict[str, Path],
    stack: np.ndarray,
    canyon_y: np.ndarray,
    rappel_y: np.ndarray,
    transform,
    crs,
    cfg: MLConfig,
) -> None:
    with rasterio.open(
        paths["features"],
        "w",
        driver="GTiff",
        height=stack.shape[1],
        width=stack.shape[2],
        count=stack.shape[0],
        dtype="float32",
        crs=crs,
        transform=transform,
        **_cache_storage_options(cfg),
    ) as dst:
        for i in range(stack.shape[0]):
            dst.write(stack[i], i + 1)

    _write_cache_raster(paths["canyon"], canyon_y, transform, crs, cfg, dtype="float32")
    _write_cache_raster(paths["rappel"], rappel_y, transform, crs, cfg, dtype="float32")


def _prepare_cache_entry(entry: ManifestEntry, cache_dir: str | Path, cfg: MLConfig) -> str:
    cache_dir = Path(cache_dir)
    paths = _cache_paths(cache_dir, entry.dem_id)
    paths["dir"].mkdir(parents=True, exist_ok=True)

    log(f"preparing cache for {entry.dem_path}")
    elev, transform, crs, pixel_size = read_dem(entry.dem_path)
    stack = build_feature_stack(
        elev, transform, pixel_size, cfg, dem_path=entry.dem_path
    )  # (C, H, W)
    canyon_y, rappel_y = build_labels_from_kml_paths(
        entry.kml_paths,
        transform,
        crs,
        elev.shape,
        pixel_size,
        cfg,
        label_sigma_m=entry.label_sigma_m,
        rappel_sigma_m=entry.rappel_sigma_m,
    )
    valid_dem = np.isfinite(elev)
    canyon_y = np.where(valid_dem, canyon_y, 0.0).astype(np.float32, copy=False)
    rappel_y = np.where(valid_dem, rappel_y, 0.0).astype(np.float32, copy=False)

    if _cache_format(cfg) == "zarr":
        _write_zarr_cache(paths, stack, canyon_y, rappel_y, cfg)
    else:
        _write_geotiff_cache(paths, stack, canyon_y, rappel_y, transform, crs, cfg)

    paths["meta"].write_text(
        json.dumps(
            {
                "dem_path": entry.dem_path,
                "kml_path": entry.kml_path,
                "kml_paths": list(entry.kml_paths),
                "pixel_size": pixel_size,
                "shape": list(elev.shape),
                "crs": str(crs),
                "label_sigma_m": entry.label_sigma_m,
                "rappel_sigma_m": entry.rappel_sigma_m,
                "precision_tier": entry.precision_tier,
                "feature_stack": feature_stack_signature(cfg),
                "cache_storage": _cache_storage_signature(cfg),
            },
            indent=2,
        )
    )
    return entry.dem_id


def _cache_worker_init() -> None:
    cache_root = Path(
        os.environ.get(
            "NUMBA_CACHE_DIR",
            str(Path(tempfile.gettempdir()) / "canyongis-numba-cache" / f"worker-{os.getpid()}"),
        )
    )
    cache_root.mkdir(parents=True, exist_ok=True)
    os.environ["NUMBA_CACHE_DIR"] = str(cache_root)
    # Import after NUMBA_CACHE_DIR is configured. By default each worker gets an
    # isolated cache; callers can opt into a shared cache by setting NUMBA_CACHE_DIR.
    import canyongis.hydrology  # noqa: F401


def _prewarm_shared_hydrology_numba_cache(cfg: MLConfig) -> None:
    if not cfg.flow_fill_depressions:
        return
    cache_dir = os.environ.get("NUMBA_CACHE_DIR")
    if not cache_dir:
        return

    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    from rasterio.transform import from_origin

    from ..hydrology import compute_flow_accum

    yy, xx = np.mgrid[:64, :64].astype(np.float32)
    elev = (100.0 - yy - 0.1 * xx).astype(np.float32)
    transform = from_origin(0.0, 64.0, 1.0, 1.0)
    compute_flow_accum(elev, transform, fill_depressions=True)


def _prepare_cache_entry_worker(entry: ManifestEntry, cache_dir: str | Path, cfg: MLConfig) -> str:
    try:
        return _prepare_cache_entry(entry, cache_dir, cfg)
    except Exception as exc:
        tb = traceback.format_exc()
        raise RuntimeError(
            f"cache preparation failed for {entry.dem_path}: {type(exc).__name__}: {exc}\n{tb}"
        ) from None


def prepare_cache(manifest: list[ManifestEntry], cache_dir: str | Path, cfg: MLConfig) -> None:
    """Precompute feature stacks and label rasters for every manifest entry.

    Writes one subdirectory per DEM under `cache_dir` containing:
      - features.tif (cfg.in_channels-band float32)
      - canyon_label.tif (float32, soft label)
      - rappel_label.tif (float32, heatmap)
      - meta.json (transform, crs, pixel_size, shape)
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    pending: list[ManifestEntry] = []
    seen_dem_ids: set[str] = set()
    for entry in manifest:
        if entry.dem_id in seen_dem_ids:
            log(f"cache duplicate skipped: {entry.dem_id} ({entry.dem_path})")
            continue
        seen_dem_ids.add(entry.dem_id)

        paths = _cache_paths(cache_dir, entry.dem_id)
        paths["dir"].mkdir(parents=True, exist_ok=True)
        feature_count = _cache_feature_count(paths, cfg)
        feature_signature = _cache_feature_signature(paths)
        expected_signature = feature_stack_signature(cfg)
        storage_matches = _cache_storage_matches(paths, cfg)
        if feature_count is not None:
            if (
                feature_count == cfg.in_channels
                and feature_signature == expected_signature
                and storage_matches
            ):
                log(f"cache hit: {entry.dem_id} ({entry.dem_path})")
                continue
            if feature_count != cfg.in_channels:
                log(
                    f"cache stale: {entry.dem_id} has {feature_count} feature bands, "
                    f"expected {cfg.in_channels}; rebuilding"
                )
            elif feature_signature != expected_signature:
                log(f"cache stale: {entry.dem_id} feature stack settings changed; rebuilding")
            else:
                log(f"cache stale: {entry.dem_id} cache storage settings changed; rebuilding")
        pending.append(entry)

    if not pending:
        return

    cache_workers = max(1, int(cfg.cache_workers))
    if cache_workers == 1 or len(pending) == 1:
        for entry in pending:
            _prepare_cache_entry(entry, cache_dir, cfg)
        return

    workers = min(cache_workers, len(pending))
    log(f"preparing {len(pending)} DEM caches with {workers} workers")
    _prewarm_shared_hydrology_numba_cache(cfg)
    mp_context = mp.get_context("spawn")
    with ProcessPoolExecutor(
        max_workers=workers,
        mp_context=mp_context,
        initializer=_cache_worker_init,
    ) as pool:
        futures = {
            pool.submit(_prepare_cache_entry_worker, entry, cache_dir, cfg): entry
            for entry in pending
        }
        for future in as_completed(futures):
            entry = futures[future]
            dem_id = future.result()
            log(f"cache ready: {dem_id} ({entry.dem_path})")


# -----------------------------------------------------------------------------
# Tile index
# -----------------------------------------------------------------------------


@dataclass
class TileRecord:
    dem_id: str
    row: int
    col: int
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

    for dem_id in _list_cached_dem_ids(cache_dir):
        paths = _cache_paths(cache_dir, dem_id)
        if _cache_format(cfg) == "zarr":
            if not paths["zarr"].exists():
                continue
            import zarr

            root = zarr.open_group(paths["zarr"], mode="r")
            canyon_full = np.asarray(root["canyon"][:], dtype=np.float32)
            h, w = canyon_full.shape
        else:
            if not paths["canyon"].exists():
                continue
            with rasterio.open(paths["canyon"]) as src:
                h, w = src.height, src.width
                canyon_full = src.read(1)  # small enough to hold one at a time

        for r in range(0, h - tile + 1, stride):
            for c in range(0, w - tile + 1, stride):
                patch = canyon_full[r : r + tile, c : c + tile]
                has_pos = bool(patch.max() > cfg.positive_threshold)
                records.append(TileRecord(dem_id=dem_id, row=r, col=c, has_positive=has_pos))
    return records


def read_cache_feature_tile(cache_dir: str | Path, rec: TileRecord, cfg: MLConfig) -> np.ndarray:
    paths = _cache_paths(Path(cache_dir), rec.dem_id)
    tile = cfg.tile_size
    if _cache_format(cfg) == "zarr":
        import zarr

        root = zarr.open_group(paths["zarr"], mode="r")
        return np.asarray(
            root["features"][:, rec.row : rec.row + tile, rec.col : rec.col + tile],
            dtype=np.float32,
        )

    win = Window(rec.col, rec.row, tile, tile)
    with rasterio.open(paths["features"]) as src:
        return src.read(window=win).astype(np.float32)


# -----------------------------------------------------------------------------
# Spatial K-fold split (grouped by DEM — whole canyons held out per fold)
# -----------------------------------------------------------------------------


class SpatialKFoldSplitter:
    """Group K-fold by DEM: every tile from one DEM lands in the same fold.

    Holding out whole DEMs is the only honest way to measure cross-canyon
    generalization. Splitting at the super-tile level leaks every DEM into
    train and val, so val_cldice rewards memorization instead of transfer.
    """

    def __init__(self, n_folds: int, seed: int = 0) -> None:
        if n_folds <= 0:
            raise ValueError("n_folds must be positive")
        self.n_folds = n_folds
        self.seed = seed

    def _sort_key(self, dem_id: str) -> str:
        h = hashlib.sha1(f"{self.seed}:{dem_id}".encode()).hexdigest()
        return h

    def _assign_dem_folds(self, dem_ids: list[str]) -> dict[str, int]:
        shuffled = sorted(set(dem_ids), key=self._sort_key)
        return {dem_id: i % self.n_folds for i, dem_id in enumerate(shuffled)}

    def split(self, records: list[TileRecord]) -> list[tuple[list[int], list[int]]]:
        """Return [(train_idx, val_idx), ...] for each of n_folds."""
        dem_folds = self._assign_dem_folds([r.dem_id for r in records])
        assignments = [dem_folds[r.dem_id] for r in records]
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
        self._src_cache: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self.indices)

    def _sources(self, dem_id: str) -> dict[str, Any]:
        # Lazy-open; each worker has its own instance (DataLoader forks).
        if dem_id not in self._src_cache:
            paths = _cache_paths(self.cache_dir, dem_id)
            if _cache_format(self.cfg) == "zarr":
                import zarr

                root = zarr.open_group(paths["zarr"], mode="r")
                self._src_cache[dem_id] = {
                    "format": "zarr",
                    "features": root["features"],
                    "canyon": root["canyon"],
                    "rappel": root["rappel"],
                }
            else:
                self._src_cache[dem_id] = {
                    "format": "geotiff",
                    "features": rasterio.open(paths["features"]),
                    "canyon": rasterio.open(paths["canyon"]),
                    "rappel": rasterio.open(paths["rappel"]),
                }
        return self._src_cache[dem_id]

    def __getitem__(self, idx: int) -> dict[str, torch.Tensor]:
        rec = self.records[self.indices[idx]]
        tile = self.cfg.tile_size
        src = self._sources(rec.dem_id)

        if src["format"] == "zarr":
            row = rec.row
            col = rec.col
            feat = np.asarray(
                src["features"][:, row : row + tile, col : col + tile],
                dtype=np.float32,
            )
            canyon = np.asarray(src["canyon"][row : row + tile, col : col + tile], dtype=np.float32)
            rappel = np.asarray(src["rappel"][row : row + tile, col : col + tile], dtype=np.float32)
        else:
            win = Window(rec.col, rec.row, tile, tile)
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
                if hasattr(s, "close"):
                    s.close()
        self._src_cache.clear()


def make_weighted_sampler(
    records: list[TileRecord], indices: list[int], cfg: MLConfig
) -> torch.utils.data.WeightedRandomSampler:
    """positive_tile_ratio fraction of draws should come from positive tiles."""
    num_samples = len(indices)
    if cfg.train_samples_per_epoch > 0:
        num_samples = min(num_samples, cfg.train_samples_per_epoch)

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
        weights=weights.tolist(), num_samples=num_samples, replacement=True
    )
