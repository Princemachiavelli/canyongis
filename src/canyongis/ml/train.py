from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import torch
from torch.utils.data import DataLoader

from ..io import log
from .augment import build_eval_transform, build_train_transform
from .config import MLConfig
from .dataset import (
    ManifestEntry,
    SpatialKFoldSplitter,
    TileDataset,
    TileRecord,
    _cache_paths,
    build_tile_index,
    load_manifest,
    make_weighted_sampler,
    prepare_cache,
)
from .device import amp_enabled, batch_size_for, describe_device, get_device, grad_scaler_enabled
from .features import compute_norm_stats
from .losses import MultiTaskLoss, soft_cldice_metric, soft_dice_metric
from .model import build_model


def _sample_norm_stats(
    cache_dir: Path,
    train_records: list[TileRecord],
    cfg: MLConfig,
    max_samples: int = 64,
) -> dict[str, list[float]]:
    """Pull up to `max_samples` tile patches and compute per-channel stats."""
    from rasterio.windows import Window

    rng = np.random.default_rng(0)
    idxs = rng.choice(len(train_records), size=min(max_samples, len(train_records)), replace=False)
    stacks: list[np.ndarray] = []
    for i in idxs:
        rec = train_records[int(i)]
        paths = _cache_paths(cache_dir, rec.dem_id)
        win = Window(rec.col, rec.row, cfg.tile_size, cfg.tile_size)
        with rasterio.open(paths["features"]) as src:
            arr = src.read(window=win).astype(np.float32)
        stacks.append(arr)
    return compute_norm_stats(stacks)


def _build_optimizer_scheduler(
    model: torch.nn.Module, cfg: MLConfig, steps_per_epoch: int
) -> tuple[torch.optim.Optimizer, torch.optim.lr_scheduler.LRScheduler]:
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)

    warmup_steps = cfg.warmup_epochs * steps_per_epoch
    total_steps = cfg.epochs * steps_per_epoch

    def lr_lambda(step: int) -> float:
        if step < warmup_steps:
            return float(step + 1) / max(warmup_steps, 1)
        progress = (step - warmup_steps) / max(total_steps - warmup_steps, 1)
        return 0.5 * (1.0 + math.cos(math.pi * progress))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_lambda)
    return opt, sched


def _to_device(batch: dict[str, Any], device: torch.device) -> dict[str, Any]:
    return {k: v.to(device, non_blocking=True) if hasattr(v, "to") else v for k, v in batch.items()}


def train_fold(
    fold_idx: int,
    cache_dir: Path,
    records: list[TileRecord],
    train_idx: list[int],
    val_idx: list[int],
    cfg: MLConfig,
    out_dir: Path,
) -> dict[str, Any]:
    device = get_device()
    use_amp = amp_enabled(device)
    use_grad_scaler = grad_scaler_enabled(device)
    batch_size = batch_size_for(device, cfg)
    log(
        f"[fold {fold_idx}] {describe_device(device)} "
        f"batch={batch_size} amp={use_amp} grad_scaler={use_grad_scaler}"
    )

    train_records = [records[i] for i in train_idx]
    stats = _sample_norm_stats(cache_dir, train_records, cfg)
    (out_dir / f"fold{fold_idx}").mkdir(parents=True, exist_ok=True)
    (out_dir / f"fold{fold_idx}" / "norm.json").write_text(json.dumps(stats, indent=2))

    train_ds = TileDataset(
        cache_dir=cache_dir,
        records=records,
        cfg=cfg,
        norm_stats=stats,
        indices=train_idx,
        transform=build_train_transform(cfg),
    )
    val_ds = TileDataset(
        cache_dir=cache_dir,
        records=records,
        cfg=cfg,
        norm_stats=stats,
        indices=val_idx,
        transform=build_eval_transform(cfg),
    )

    sampler = make_weighted_sampler(records, train_idx, cfg)
    train_loader = DataLoader(
        train_ds, batch_size=batch_size, sampler=sampler,
        num_workers=cfg.num_workers, pin_memory=(device.type == "cuda"),
    )
    val_loader = DataLoader(
        val_ds, batch_size=batch_size, shuffle=False,
        num_workers=cfg.num_workers, pin_memory=(device.type == "cuda"),
    )

    model = build_model(cfg).to(device)
    loss_fn = MultiTaskLoss(cfg).to(device)
    steps_per_epoch = max(len(train_loader) // max(cfg.grad_accum_steps, 1), 1)
    opt, sched = _build_optimizer_scheduler(model, cfg, steps_per_epoch)
    scaler = torch.amp.GradScaler("cuda", enabled=use_grad_scaler)

    best_cldice = -1.0
    bad_epochs = 0
    history: list[dict[str, float]] = []

    for epoch in range(cfg.epochs):
        if epoch == 0:
            model.freeze_encoder(True)
        if epoch == cfg.freeze_encoder_epochs:
            model.freeze_encoder(False)
            log(f"[fold {fold_idx}] unfroze encoder at epoch {epoch}")

        model.train()
        running = 0.0
        opt.zero_grad(set_to_none=True)
        accum = cfg.grad_accum_steps
        for step, batch in enumerate(train_loader):
            batch = _to_device(batch, device)
            with torch.autocast(device_type=device.type, enabled=use_amp):
                out = model(batch["image"])
                loss_dict = loss_fn(out, {"canyon": batch["canyon"], "rappel": batch["rappel"]})
                loss = loss_dict["loss"] / accum
            scaler.scale(loss).backward()
            if (step + 1) % accum == 0:
                scaler.step(opt)
                scaler.update()
                opt.zero_grad(set_to_none=True)
                sched.step()
            running += float(loss_dict["loss"].detach())
        train_loss = running / max(len(train_loader), 1)

        model.eval()
        val_cldice = 0.0
        val_dice = 0.0
        n_batches = 0
        with torch.no_grad():
            for batch in val_loader:
                batch = _to_device(batch, device)
                out = model(batch["image"])
                val_cldice += float(soft_cldice_metric(out["canyon_logits"], batch["canyon"]))
                val_dice += float(soft_dice_metric(out["canyon_logits"], batch["canyon"]))
                n_batches += 1
        if n_batches > 0:
            val_cldice /= n_batches
            val_dice /= n_batches

        log(
            f"[fold {fold_idx}] ep{epoch:03d} "
            f"train_loss={train_loss:.4f} val_cldice={val_cldice:.4f} val_dice={val_dice:.4f}"
        )
        history.append({"epoch": epoch, "train_loss": train_loss, "val_cldice": val_cldice, "val_dice": val_dice})

        if val_cldice > best_cldice:
            best_cldice = val_cldice
            bad_epochs = 0
            torch.save(
                {
                    "model_state": model.state_dict(),
                    "cfg": cfg.__dict__,
                    "norm_stats": stats,
                    "epoch": epoch,
                    "val_cldice": val_cldice,
                },
                out_dir / f"fold{fold_idx}" / "best.pt",
            )
        else:
            bad_epochs += 1
            if bad_epochs >= cfg.early_stop_patience:
                log(f"[fold {fold_idx}] early stopping at epoch {epoch}")
                break

    train_ds.close()
    val_ds.close()
    del model
    if device.type == "cuda":
        torch.cuda.empty_cache()

    (out_dir / f"fold{fold_idx}" / "history.json").write_text(json.dumps(history, indent=2))
    return {"fold": fold_idx, "best_cldice": best_cldice, "history": history}


def run_training(cfg: MLConfig) -> dict[str, Any]:
    """Top-level training driver: manifest → K folds → aggregated report."""
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "cache"

    manifest: list[ManifestEntry] = load_manifest(cfg.manifest_path)
    prepare_cache(manifest, cache_dir, cfg)
    records = build_tile_index(cache_dir, cfg)
    if not records:
        raise RuntimeError("tile index is empty — check manifest and cache")
    log(f"tile index: {len(records)} tiles across {len({r.dem_id for r in records})} DEMs")

    splitter = SpatialKFoldSplitter(n_folds=cfg.n_folds, seed=cfg.spatial_seed)
    folds = splitter.split(records)

    results: list[dict[str, Any]] = []
    for k, (tr, va) in enumerate(folds):
        if not tr or not va:
            log(f"[fold {k}] skipping (empty train or val)")
            continue
        results.append(train_fold(k, cache_dir, records, tr, va, cfg, out_dir))

    summary = {
        "mean_cldice": float(np.mean([r["best_cldice"] for r in results])) if results else 0.0,
        "folds": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"done. mean val clDice = {summary['mean_cldice']:.4f}")
    return summary
