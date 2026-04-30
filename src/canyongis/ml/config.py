from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MLConfig:
    # --- Feature stack (radii in meters; reused from old terrain thresholds) ---
    tpi_radius_m: float = 200.0
    local_relief_radius_m: float = 20.0
    detrend_radius_m: float = 500.0
    hand_clip_m: float = 100.0
    flow_accum_stream_threshold: int = 200
    flow_fill_depressions: bool = True
    use_geomorphons: bool = False
    geomorphon_search_m: tuple[float, ...] = (30.0,)
    geomorphon_skip_m: float = 0.0
    geomorphon_flat_deg: float = 1.0
    geomorphon_dist_m: float = 0.0
    geomorphon_auto_generate: bool = True

    # --- Labels ---
    label_sigma_m: float = 5.0
    rappel_sigma_m: float = 3.0
    label_floor: float = 0.05

    # --- Tiling ---
    tile_size: int = 256
    train_stride: int = 128
    predict_overlap: int = 64

    # --- Sampling ---
    positive_tile_ratio: float = 0.8
    positive_threshold: float = 0.3

    # --- Split ---
    super_tile_size: int = 512
    n_folds: int = 5
    spatial_seed: int = 0

    # --- Model ---
    encoder_name: str = "resnet18"
    encoder_weights: str | None = "imagenet"
    in_channels: int = 7
    decoder_channels: tuple[int, ...] = (256, 128, 64, 32, 16)

    # --- Loss ---
    canyon_loss_weight: float = 1.0
    rappel_loss_weight: float = 0.3
    bce_cldice_ratio: float = 0.5
    pu_prior: float = 0.01
    focal_gamma: float = 2.0
    cldice_iter: int = 3
    cldice_smooth: float = 1.0

    # --- Training ---
    batch_size_cuda: int = 6
    batch_size_mps: int = 4
    batch_size_cpu: int = 2
    grad_accum_steps: int = 2
    epochs: int = 60
    warmup_epochs: int = 5
    freeze_encoder_epochs: int = 20
    lr: float = 1e-4
    weight_decay: float = 1e-4
    early_stop_patience: int = 10
    cache_workers: int = 2
    num_workers: int = 2
    dataloader_prefetch_factor: int = 2
    dataloader_persistent_workers: bool = True
    train_samples_per_epoch: int = 0
    val_samples_per_epoch: int = 0
    train_log_interval_batches: int = 100

    # --- Augmentation ---
    elastic_transform_prob: float = 0.3
    elev_noise_std_m: float = 0.3
    elev_drift_m: float = 5.0
    random_erasing_prob: float = 0.05

    # --- Inference ---
    prob_threshold: float = 0.5
    min_centerline_length_m: float = 10.0
    rappel_peak_min_distance_m: float = 4.0

    # --- Route scoring (carried over from old Config) ---
    min_total_descent: float = 50.0
    min_route_grade: float = 0.04
    min_cliff_count: int = 1

    # --- Rendering ---
    render_folium: bool = True
    folium_max_points: int = 500

    # --- I/O: manifest of {kml_path, dem_path, label_sigma_m?, precision_tier?} ---
    manifest_path: str = ""
    out_dir: str = ""
    checkpoint_path: str = ""
    dem_path: str = ""

    # Per-channel normalization stats path (written at training time, read at inference).
    norm_stats_path: str = "norm.json"

    def channel_names(self) -> list[str]:
        return [
            "elev_detrended",
            "slope_deg",
            "tpi",
            "local_relief",
            "hand_log1p",
            "flow_accum_log1p",
            "distance_to_drainage_log1p",
            *self.geomorphon_channel_names(),
        ]

    def geomorphon_channel_names(self) -> list[str]:
        if not self.use_geomorphons:
            return []
        names: list[str] = []
        for search_m in self.geomorphon_search_m:
            scale = f"{search_m:g}m"
            names.extend(
                [
                    f"geomorphon_{scale}_ridge_spur",
                    f"geomorphon_{scale}_hollow_valley",
                    f"geomorphon_{scale}_positive",
                    f"geomorphon_{scale}_negative",
                    f"geomorphon_{scale}_intensity",
                ]
            )
        return names

    def __post_init__(self) -> None:
        expected = len(self.channel_names())
        if self.in_channels == 7 and expected != 7:
            self.in_channels = expected
        if self.in_channels != expected:
            raise ValueError(
                f"in_channels={self.in_channels} does not match channel_names count {expected}"
            )
