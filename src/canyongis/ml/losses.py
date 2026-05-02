from __future__ import annotations

import torch
import torch.nn.functional as F
from torch import nn

from .config import MLConfig


def soft_bce_pu(logits: torch.Tensor, target: torch.Tensor, pi: float) -> torch.Tensor:
    """BCE with positive-unlabeled down-weighting.

    target is the soft label in [0, 1]. We treat `target` as the "positive posterior"
    and (1 - target) as a weight on the negative term — but further scale the negative
    term by (1 - pi), so that fully-unlabeled pixels (target == 0) receive weight
    (1 - pi) instead of 1. This prevents unmapped canyons from acting as strong
    negatives when pi is small.
    """
    log_p = F.logsigmoid(logits)
    log_1mp = F.logsigmoid(-logits)
    pos_term = -target * log_p
    neg_term = -(1.0 - target) * log_1mp * (1.0 - pi)
    return (pos_term + neg_term).mean()


def soft_dice(logits: torch.Tensor, target: torch.Tensor, smooth: float = 1.0) -> torch.Tensor:
    p = torch.sigmoid(logits)
    dims = tuple(range(1, p.ndim))
    num = 2.0 * (p * target).sum(dim=dims) + smooth
    den = (p + target).sum(dim=dims) + smooth
    return 1.0 - (num / den).mean()


def _soft_skeleton(p: torch.Tensor, iters: int) -> torch.Tensor:
    """Soft morphological skeletonization (per Shit et al. 2021, eq. 7).

    Iteratively erode + open, subtract to accumulate a soft skeleton approximation.
    Operates on (B, 1, H, W) tensors in [0, 1].
    """
    def erode(x):
        return -F.max_pool2d(-x, kernel_size=3, stride=1, padding=1)

    def dilate(x):
        return F.max_pool2d(x, kernel_size=3, stride=1, padding=1)

    img = p
    skel = F.relu(img - dilate(erode(img)))
    for _ in range(iters):
        img = erode(img)
        skel = skel + F.relu(img - dilate(erode(img))) * (1.0 - skel)
    return skel


def soft_cldice(
    logits: torch.Tensor,
    target: torch.Tensor,
    iters: int = 3,
    smooth: float = 1.0,
) -> torch.Tensor:
    """Topology-preserving loss for thin tubular structures (Shit et al. 2021)."""
    p = torch.sigmoid(logits)
    # Ensure channel dim for pooling ops.
    if p.ndim == 3:
        p = p.unsqueeze(1)
        target = target.unsqueeze(1)
    skel_p = _soft_skeleton(p, iters)
    skel_t = _soft_skeleton(target, iters)

    t_prec = (skel_p * target).sum(dim=(1, 2, 3)) + smooth
    t_prec = t_prec / (skel_p.sum(dim=(1, 2, 3)) + smooth)

    t_rec = (skel_t * p).sum(dim=(1, 2, 3)) + smooth
    t_rec = t_rec / (skel_t.sum(dim=(1, 2, 3)) + smooth)

    cl = 2.0 * t_prec * t_rec / (t_prec + t_rec + 1e-8)
    return 1.0 - cl.mean()


def gaussian_focal_loss(
    logits: torch.Tensor,
    target: torch.Tensor,
    alpha: float = 2.0,
    beta: float = 4.0,
) -> torch.Tensor:
    """Focal loss for soft Gaussian heatmap targets (Law & Deng 2018; Zhou et al. 2019).

    Standard focal loss assumes hard binary targets — its `p_t = p·y + (1-p)·(1-y)`
    collapses to 0.5 wherever `y ≈ 0.5`, so the `(1-p_t)^γ` modulator becomes a
    constant on Gaussian-smoothed labels and stops down-weighting easy examples.

    The CornerNet/CenterNet variant separates the focal modulator (`p^α` on the
    negative term, `(1-p)^α` on the positive term) from a heatmap-aware spatial
    down-weighting `(1-y)^β` on the negative term, so pixels near the true peak —
    which are partially-positive Gaussian context, not true negatives — are
    penalized less.
    """
    p = torch.sigmoid(logits)
    log_p = F.logsigmoid(logits)
    log_1mp = F.logsigmoid(-logits)
    pos = -target * (1.0 - p).pow(alpha) * log_p
    neg = -(1.0 - target).pow(beta) * p.pow(alpha) * log_1mp
    return (pos + neg).mean()


class MultiTaskLoss(nn.Module):
    def __init__(self, cfg: MLConfig) -> None:
        super().__init__()
        self.cfg = cfg

    def forward(
        self,
        outputs: dict[str, torch.Tensor],
        targets: dict[str, torch.Tensor],
    ) -> dict[str, torch.Tensor]:
        c_logits = outputs["canyon_logits"]
        r_logits = outputs["rappel_logits"]
        c_target = targets["canyon"]
        r_target = targets["rappel"]

        bce = soft_bce_pu(c_logits, c_target, pi=self.cfg.pu_prior)
        cld = soft_cldice(
            c_logits, c_target, iters=self.cfg.cldice_iter, smooth=self.cfg.cldice_smooth
        )
        canyon = (
            self.cfg.bce_cldice_ratio * bce + (1.0 - self.cfg.bce_cldice_ratio) * cld
        )
        rappel = gaussian_focal_loss(
            r_logits, r_target, alpha=self.cfg.focal_gamma, beta=self.cfg.focal_beta
        )

        total = self.cfg.canyon_loss_weight * canyon + self.cfg.rappel_loss_weight * rappel
        return {
            "loss": total,
            "loss_canyon": canyon,
            "loss_rappel": rappel,
            "loss_bce": bce,
            "loss_cldice": cld,
        }


@torch.no_grad()
def soft_dice_metric(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """Utility for monitoring — returns Dice score (higher = better) in [0, 1]."""
    return 1.0 - soft_dice(logits, target)


@torch.no_grad()
def soft_cldice_metric(logits: torch.Tensor, target: torch.Tensor, iters: int = 3) -> torch.Tensor:
    return 1.0 - soft_cldice(logits, target, iters=iters)
