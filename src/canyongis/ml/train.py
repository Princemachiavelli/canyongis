from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
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
    build_tile_index,
    load_manifest,
    make_weighted_sampler,
    prepare_cache,
    read_cache_feature_tile,
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
    rng = np.random.default_rng(0)
    idxs = rng.choice(len(train_records), size=min(max_samples, len(train_records)), replace=False)
    stacks: list[np.ndarray] = []
    for i in idxs:
        rec = train_records[int(i)]
        stacks.append(read_cache_feature_tile(cache_dir, rec, cfg))
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


def _dataloader_kwargs(cfg: MLConfig, device: torch.device) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "num_workers": cfg.num_workers,
        "pin_memory": device.type == "cuda",
    }
    if cfg.num_workers > 0:
        kwargs["prefetch_factor"] = max(1, cfg.dataloader_prefetch_factor)
        kwargs["persistent_workers"] = cfg.dataloader_persistent_workers
    return kwargs


def _sample_val_indices(val_idx: list[int], cfg: MLConfig, fold_idx: int) -> list[int]:
    if cfg.val_samples_per_epoch <= 0 or cfg.val_samples_per_epoch >= len(val_idx):
        return val_idx
    rng = np.random.default_rng(cfg.spatial_seed + fold_idx)
    sampled = rng.choice(val_idx, size=cfg.val_samples_per_epoch, replace=False)
    return [int(i) for i in sampled]


def _log_batch_progress(
    fold_idx: int,
    epoch: int,
    phase: str,
    done: int,
    total: int,
    batch_size: int,
    started_at: float,
) -> None:
    if done <= 0:
        return
    elapsed = max(time.perf_counter() - started_at, 1e-6)
    batches_per_s = done / elapsed
    tiles_per_s = (done * batch_size) / elapsed
    log(
        f"[fold {fold_idx}] ep{epoch:03d} {phase} "
        f"{done}/{total} batches ({batches_per_s:.2f} batch/s, {tiles_per_s:.0f} tile/s)"
    )


_LOSS_COMPONENT_KEYS: tuple[str, ...] = (
    "loss",
    "loss_canyon",
    "loss_rappel",
    "loss_bce",
    "loss_cldice",
)


def _log_loss_components(
    fold_idx: int,
    epoch: int,
    phase: str,
    sums: dict[str, torch.Tensor],
    n: int,
) -> None:
    if n <= 0:
        return
    means = {k: float((v / n).detach().cpu()) for k, v in sums.items()}
    log(
        f"[fold {fold_idx}] ep{epoch:03d} {phase} losses "
        f"total={means['loss']:.4f} "
        f"canyon={means['loss_canyon']:.4f} "
        f"rappel={means['loss_rappel']:.4f} "
        f"bce={means['loss_bce']:.4f} "
        f"cldice={means['loss_cldice']:.4f}"
    )


def _sync_for_timing(device: torch.device) -> None:
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    elif device.type == "mps" and hasattr(torch, "mps") and hasattr(torch.mps, "synchronize"):
        torch.mps.synchronize()


def _timing_enabled(cfg: MLConfig) -> bool:
    return cfg.train_timing_batches > 0


def _timing_collect_batch(step: int, cfg: MLConfig) -> bool:
    if not _timing_enabled(cfg):
        return False
    warmup = max(0, cfg.train_timing_warmup_batches)
    return warmup <= step < warmup + cfg.train_timing_batches


def _timing_sync_after_batch(step: int, cfg: MLConfig) -> bool:
    if not _timing_enabled(cfg):
        return False
    return step < max(0, cfg.train_timing_warmup_batches) + cfg.train_timing_batches


def _record_timing(samples: dict[str, list[float]], key: str, value_ms: float) -> None:
    samples.setdefault(key, []).append(value_ms)


def _elapsed_ms(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def _timed_elapsed_ms(started_at: float, device: torch.device) -> float:
    _sync_for_timing(device)
    return _elapsed_ms(started_at)


def _log_timing_summary(
    fold_idx: int,
    epoch: int,
    phase: str,
    samples: dict[str, list[float]],
    cfg: MLConfig,
) -> None:
    totals = samples.get("total")
    if not totals:
        return

    labels = {
        "data_wait": "data_wait",
        "h2d": "h2d",
        "forward_loss": "forward_loss",
        "forward_metric": "forward_metric",
        "backward": "backward",
        "optimizer": "optimizer",
        "accum": "accum",
        "total": "total",
    }
    parts = [
        f"{labels[key]}={float(np.mean(values)):.1f}ms"
        for key, values in samples.items()
        if values and key in labels
    ]
    log(
        f"[fold {fold_idx}] ep{epoch:03d} {phase} timing "
        f"{len(totals)} batches after {max(0, cfg.train_timing_warmup_batches)} warmup: "
        + " ".join(parts)
    )


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
    sampled_val_idx = _sample_val_indices(val_idx, cfg, fold_idx)
    val_ds = TileDataset(
        cache_dir=cache_dir,
        records=records,
        cfg=cfg,
        norm_stats=stats,
        indices=sampled_val_idx,
        transform=build_eval_transform(cfg),
    )

    sampler = make_weighted_sampler(records, train_idx, cfg)
    loader_kwargs = _dataloader_kwargs(cfg, device)
    train_loader = DataLoader(
        train_ds,
        batch_size=batch_size,
        sampler=sampler,
        **loader_kwargs,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=batch_size,
        shuffle=False,
        **loader_kwargs,
    )
    log(
        f"[fold {fold_idx}] train={len(train_ds)} tiles "
        f"({len(train_loader)} batches sampled/epoch) "
        f"val={len(val_ds)} tiles ({len(val_loader)} batches) "
        f"workers={cfg.num_workers} prefetch={cfg.dataloader_prefetch_factor} "
        f"persistent={cfg.dataloader_persistent_workers} "
        f"elastic_p={cfg.elastic_transform_prob}"
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
        running_losses: dict[str, torch.Tensor] = {
            k: torch.zeros((), device=device) for k in _LOSS_COMPONENT_KEYS
        }
        train_batches = 0
        opt.zero_grad(set_to_none=True)
        accum = cfg.grad_accum_steps
        train_started = time.perf_counter()
        train_timing: dict[str, list[float]] = {}
        train_iter = iter(train_loader)
        batch_gap_started = time.perf_counter()
        for step in range(len(train_loader)):
            batch_started = batch_gap_started
            try:
                batch = next(train_iter)
            except StopIteration:
                break
            batch_arrived = time.perf_counter()
            collect_timing = _timing_collect_batch(step, cfg)
            if collect_timing:
                _record_timing(train_timing, "data_wait", (batch_arrived - batch_started) * 1000.0)

            if collect_timing:
                _sync_for_timing(device)
                segment_started = time.perf_counter()
            batch = _to_device(batch, device)
            if collect_timing:
                _record_timing(train_timing, "h2d", _timed_elapsed_ms(segment_started, device))

            if collect_timing:
                segment_started = time.perf_counter()
            with torch.autocast(device_type=device.type, enabled=use_amp):
                out = model(batch["image"])
                loss_dict = loss_fn(out, {"canyon": batch["canyon"], "rappel": batch["rappel"]})
                loss = loss_dict["loss"] / accum
            if collect_timing:
                _record_timing(
                    train_timing,
                    "forward_loss",
                    _timed_elapsed_ms(segment_started, device),
                )

            if collect_timing:
                segment_started = time.perf_counter()
            scaler.scale(loss).backward()
            if collect_timing:
                _record_timing(train_timing, "backward", _timed_elapsed_ms(segment_started, device))

            optimizer_ms = 0.0
            if (step + 1) % accum == 0:
                if collect_timing:
                    segment_started = time.perf_counter()
                scaler.step(opt)
                scaler.update()
                opt.zero_grad(set_to_none=True)
                sched.step()
                if collect_timing:
                    optimizer_ms = _timed_elapsed_ms(segment_started, device)
            if collect_timing:
                _record_timing(train_timing, "optimizer", optimizer_ms)

            if collect_timing:
                segment_started = time.perf_counter()
            for loss_key in _LOSS_COMPONENT_KEYS:
                running_losses[loss_key] = running_losses[loss_key] + loss_dict[loss_key].detach()
            train_batches += 1
            if collect_timing:
                _record_timing(train_timing, "accum", _timed_elapsed_ms(segment_started, device))

            done = step + 1
            if cfg.train_log_interval_batches > 0 and done % cfg.train_log_interval_batches == 0:
                _log_batch_progress(
                    fold_idx,
                    epoch,
                    "train",
                    done,
                    len(train_loader),
                    batch_size,
                    train_started,
                )
                _log_loss_components(
                    fold_idx, epoch, "train", running_losses, train_batches
                )
            if _timing_sync_after_batch(step, cfg):
                _sync_for_timing(device)
            batch_done = time.perf_counter()
            if collect_timing:
                _record_timing(train_timing, "total", (batch_done - batch_started) * 1000.0)
            batch_gap_started = batch_done
        train_components = {
            k: float((v / max(train_batches, 1)).detach().cpu())
            for k, v in running_losses.items()
        }
        train_loss = train_components["loss"]
        _log_loss_components(fold_idx, epoch, "train", running_losses, train_batches)
        _log_timing_summary(fold_idx, epoch, "train", train_timing, cfg)

        model.eval()
        val_cldice_sum = torch.zeros((), device=device)
        val_dice_sum = torch.zeros((), device=device)
        n_batches = 0
        val_started = time.perf_counter()
        val_timing: dict[str, list[float]] = {}
        val_iter = iter(val_loader)
        batch_gap_started = time.perf_counter()
        with torch.no_grad():
            for step in range(len(val_loader)):
                batch_started = batch_gap_started
                try:
                    batch = next(val_iter)
                except StopIteration:
                    break
                batch_arrived = time.perf_counter()
                collect_timing = _timing_collect_batch(step, cfg)
                if collect_timing:
                    _record_timing(
                        val_timing,
                        "data_wait",
                        (batch_arrived - batch_started) * 1000.0,
                    )

                if collect_timing:
                    _sync_for_timing(device)
                    segment_started = time.perf_counter()
                batch = _to_device(batch, device)
                if collect_timing:
                    _record_timing(val_timing, "h2d", _timed_elapsed_ms(segment_started, device))

                if collect_timing:
                    segment_started = time.perf_counter()
                out = model(batch["image"])
                cldice = soft_cldice_metric(out["canyon_logits"], batch["canyon"])
                dice = soft_dice_metric(out["canyon_logits"], batch["canyon"])
                if collect_timing:
                    _record_timing(
                        val_timing,
                        "forward_metric",
                        _timed_elapsed_ms(segment_started, device),
                    )

                if collect_timing:
                    segment_started = time.perf_counter()
                val_cldice_sum = val_cldice_sum + cldice.detach()
                val_dice_sum = val_dice_sum + dice.detach()
                if collect_timing:
                    _record_timing(val_timing, "accum", _timed_elapsed_ms(segment_started, device))

                n_batches += 1
                if (
                    cfg.train_log_interval_batches > 0
                    and n_batches % cfg.train_log_interval_batches == 0
                ):
                    _log_batch_progress(
                        fold_idx,
                        epoch,
                        "val",
                        n_batches,
                        len(val_loader),
                        batch_size,
                        val_started,
                    )
                if _timing_sync_after_batch(step, cfg):
                    _sync_for_timing(device)
                batch_done = time.perf_counter()
                if collect_timing:
                    _record_timing(val_timing, "total", (batch_done - batch_started) * 1000.0)
                batch_gap_started = batch_done
        if n_batches > 0:
            val_cldice = float((val_cldice_sum / n_batches).detach().cpu())
            val_dice = float((val_dice_sum / n_batches).detach().cpu())
        else:
            val_cldice = 0.0
            val_dice = 0.0
        _log_timing_summary(fold_idx, epoch, "val", val_timing, cfg)

        log(
            f"[fold {fold_idx}] ep{epoch:03d} "
            f"train_loss={train_loss:.4f} val_cldice={val_cldice:.4f} val_dice={val_dice:.4f}"
        )
        history.append(
            {
                "epoch": epoch,
                "train_loss": train_loss,
                "train_loss_canyon": train_components["loss_canyon"],
                "train_loss_rappel": train_components["loss_rappel"],
                "train_loss_bce": train_components["loss_bce"],
                "train_loss_cldice": train_components["loss_cldice"],
                "val_cldice": val_cldice,
                "val_dice": val_dice,
            }
        )

        if val_cldice > best_cldice:
            best_cldice = val_cldice
            bad_epochs = 0
            torch.save(
                {
                    "model_state": model.state_dict(),
                    "cfg": asdict(cfg),
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


def _write_best_checkpoint_link(out_dir: Path, best_result: dict[str, Any]) -> Path:
    best_ckpt = out_dir / f"fold{best_result['fold']}" / "best.pt"
    link_path = out_dir / "best.pt"
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir():
            raise RuntimeError(f"cannot replace directory with checkpoint symlink: {link_path}")
        link_path.unlink()
    link_path.symlink_to(os.path.relpath(best_ckpt, start=out_dir))
    return link_path


def run_training(cfg: MLConfig) -> dict[str, Any]:
    """Top-level training driver: manifest → K folds → aggregated report."""
    out_dir = Path(cfg.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg_dict = asdict(cfg)
    (out_dir / "training_config.json").write_text(json.dumps(cfg_dict, indent=2))
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

    best_result = max(results, key=lambda r: r["best_cldice"]) if results else None
    best_link = _write_best_checkpoint_link(out_dir, best_result) if best_result else None
    summary = {
        "mean_cldice": float(np.mean([r["best_cldice"] for r in results])) if results else 0.0,
        "best_fold": best_result["fold"] if best_result else None,
        "best_checkpoint": str(best_link) if best_link else None,
        "folds": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    if best_result:
        log(
            f"best checkpoint: fold {best_result['fold']} "
            f"clDice={best_result['best_cldice']:.4f} -> {best_link}"
        )
    log(f"done. mean val clDice = {summary['mean_cldice']:.4f}")
    return summary
