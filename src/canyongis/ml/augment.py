from __future__ import annotations

import albumentations as A

from .config import MLConfig


def build_train_transform(cfg: MLConfig) -> A.Compose:
    """Augmentations safe for DEM-derived multi-channel inputs.

    Forbidden:
      - Arbitrary rotation (would corrupt slope/aspect direction).
      - Brightness / contrast as normally applied — those are RGB-semantic.
    """
    return A.Compose(
        [
            A.HorizontalFlip(p=0.5),
            A.VerticalFlip(p=0.5),
            A.RandomRotate90(p=0.5),
            A.ElasticTransform(alpha=40, sigma=6, p=cfg.elastic_transform_prob),
            A.GaussNoise(std_range=(cfg.elev_noise_std_m, cfg.elev_noise_std_m), p=0.3),
            # Additive "DEM drift" on channel 0 only would need a custom transform;
            # instead we rely on z-score normalization to absorb small drifts.
            A.CoarseDropout(
                num_holes_range=(1, 4),
                hole_height_range=(16, 16),
                hole_width_range=(16, 16),
                p=cfg.random_erasing_prob,
            ),
        ],
        additional_targets={"canyon": "mask", "rappel": "mask", "canyon_weight": "mask"},
    )


def build_eval_transform(cfg: MLConfig) -> A.Compose:
    _ = cfg
    return A.Compose(
        [], additional_targets={"canyon": "mask", "rappel": "mask", "canyon_weight": "mask"}
    )
