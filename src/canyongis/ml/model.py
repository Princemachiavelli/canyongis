from __future__ import annotations

import segmentation_models_pytorch as smp
import torch
from torch import nn

from .config import MLConfig


class CanyonUNet(nn.Module):
    """SMP U-Net with two 1x1-conv heads for canyon probability and rappel heatmap.

    The underlying SMP U-Net is constructed with `classes=1` (so its final decoder
    outputs one feature map at 1x stride). We replace that single head with two
    parallel 1x1 convs operating on the decoder's penultimate features.
    """

    def __init__(self, cfg: MLConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.backbone = smp.Unet(
            encoder_name=cfg.encoder_name,
            encoder_weights=cfg.encoder_weights,
            in_channels=cfg.in_channels,
            classes=cfg.decoder_channels[-1],
            decoder_channels=list(cfg.decoder_channels),
            activation=None,
        )
        last_ch = cfg.decoder_channels[-1]
        self.head_canyon = nn.Conv2d(last_ch, 1, kernel_size=1)
        self.head_rappel = nn.Conv2d(last_ch, 1, kernel_size=1)

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        feats = self.backbone(x)  # SMP returns (B, classes, H, W) at full resolution
        return {
            "canyon_logits": self.head_canyon(feats),
            "rappel_logits": self.head_rappel(feats),
        }

    def freeze_encoder(self, freeze: bool = True) -> None:
        for p in self.backbone.encoder.parameters():
            p.requires_grad = not freeze


def build_model(cfg: MLConfig) -> CanyonUNet:
    return CanyonUNet(cfg)
