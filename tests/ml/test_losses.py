from __future__ import annotations

import torch

from canyongis.ml.config import MLConfig
from canyongis.ml.losses import (
    MultiTaskLoss,
    gaussian_focal_loss,
    soft_bce_pu,
    soft_cldice,
    soft_dice,
)


def test_soft_dice_zero_when_perfect():
    target = torch.zeros(2, 1, 8, 8)
    target[:, :, 3, :] = 1.0
    # logits that produce p ~ target via logit inversion. For clean 0/1 target, large +-logits.
    logits = (target * 2 - 1) * 12.0
    loss = soft_dice(logits, target)
    assert loss.item() < 0.05


def test_soft_dice_positive_when_mismatch():
    target = torch.zeros(1, 1, 8, 8)
    logits = torch.full_like(target, 4.0)  # predict all ones
    loss = soft_dice(logits, target)
    assert loss.item() > 0.5


def test_soft_cldice_positive_when_mismatch_gradient_finite():
    target = torch.zeros(1, 1, 16, 16)
    target[:, :, 8, :] = 1.0
    logits = torch.zeros_like(target, requires_grad=True)
    loss = soft_cldice(logits, target, iters=2)
    loss.backward()
    assert torch.isfinite(loss).item()
    assert torch.isfinite(logits.grad).all()


def test_soft_bce_pu_lower_weight_on_unlabeled():
    # Same logits, same target: unlabeled pixel (target=0) should get smaller loss
    # contribution as pi increases (PU down-weighting).
    logits = torch.full((1, 1, 4, 4), 2.0)
    target = torch.zeros_like(logits)
    low = soft_bce_pu(logits, target, pi=0.0).item()
    high = soft_bce_pu(logits, target, pi=0.9).item()
    assert high < low


def _gaussian_heatmap(size: int, sigma: float) -> torch.Tensor:
    """Single-peak Gaussian heatmap, peak == 1.0 at center."""
    coords = torch.arange(size, dtype=torch.float32)
    yy, xx = torch.meshgrid(coords, coords, indexing="ij")
    cy = cx = (size - 1) / 2.0
    d2 = (yy - cy) ** 2 + (xx - cx) ** 2
    return torch.exp(-0.5 * d2 / sigma**2).unsqueeze(0).unsqueeze(0)


def test_gaussian_focal_loss_decreases_as_peak_prediction_improves():
    # Confirms the focal modulator actually responds to the prediction at the
    # peak — the old p_t = p·y + (1-p)·(1-y) formulation collapsed to 0.5
    # wherever y ≈ 0.5, freezing the modulator on Gaussian context pixels.
    target = _gaussian_heatmap(size=17, sigma=2.0)
    losses = []
    for peak_logit in (-2.0, 0.0, 2.0, 6.0):
        logits = torch.full_like(target, -6.0)
        logits[..., 8, 8] = peak_logit
        losses.append(gaussian_focal_loss(logits, target).item())
    assert losses == sorted(losses, reverse=True), losses


def test_gaussian_focal_loss_downweights_near_peak_negatives():
    # Near-peak pixels (high target) should contribute less to the negative
    # term than far-from-peak pixels (target≈0) when the prediction is the
    # same. This is the (1-y)^β heatmap-aware down-weighting.
    logits = torch.full((1, 1, 1, 2), 2.0)  # both pixels predict positive
    near = torch.tensor([[[[0.0, 0.9]]]])  # one true negative, one near-peak
    far = torch.tensor([[[[0.0, 0.0]]]])  # both true negatives
    assert gaussian_focal_loss(logits, near).item() < gaussian_focal_loss(logits, far).item()


def test_multitask_loss_runs():
    cfg = MLConfig()
    loss_fn = MultiTaskLoss(cfg)
    out = {
        "canyon_logits": torch.randn(2, 1, 16, 16, requires_grad=True),
        "rappel_logits": torch.randn(2, 1, 16, 16, requires_grad=True),
    }
    tgt = {
        "canyon": torch.rand(2, 1, 16, 16),
        "rappel": torch.rand(2, 1, 16, 16) * 0.1,
    }
    result = loss_fn(out, tgt)
    assert torch.isfinite(result["loss"])
    result["loss"].backward()
