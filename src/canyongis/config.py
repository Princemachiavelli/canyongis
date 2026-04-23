from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Config:
    # I/O
    dem_path: str = "/path/to/dem_1m.tif"
    out_dir: str = "/path/to/output"

    # Cliff/rappel detection (meters/degrees)
    cliff_slope_min: float = 35.0
    cliff_slope_max: float = 90.0
    min_drop_height: float = 5.0
    max_drop_height: float = 200.0
    local_relief_radius: float = 20.0

    # TPI (canyon/ridge detection)
    tpi_radius: float = 200.0
    tpi_canyon_thresh: float = -5.0
    tpi_cliff_thresh: float = 8.0

    # Drainage channel
    hand_threshold: float = 20.0
    min_channel_slope: float = 2.0
    flow_accum_threshold: int = 500

    # Route scoring
    min_total_descent: float = 50.0
    min_route_grade: float = 0.04
    min_cliff_count: int = 1

    # Map rendering
    render_folium: bool = True
    folium_max_points: int = 500
