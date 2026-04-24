from __future__ import annotations

import torch

from canyongis.ml.config import MLConfig
from canyongis.ml.losses import MultiTaskLoss, soft_bce_pu, soft_cldice, soft_dice


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
