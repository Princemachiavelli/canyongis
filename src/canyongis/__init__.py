from __future__ import annotations

from typing import Any

_EXPORTS = {
    "KmlFeatures": ("validate", "KmlFeatures"),
    "MLConfig": ("ml.config", "MLConfig"),
    "ValidationReport": ("validate", "ValidationReport"),
    "build_feature_stack": ("ml.features", "build_feature_stack"),
    "build_labels_from_kml": ("ml.labels", "build_labels_from_kml"),
    "build_labels_from_kml_paths": ("ml.labels", "build_labels_from_kml_paths"),
    "compute_flow_accum": ("hydrology", "compute_flow_accum"),
    "compute_hand": ("hydrology", "compute_hand"),
    "compute_local_relief": ("terrain", "compute_local_relief"),
    "compute_slope": ("terrain", "compute_slope"),
    "compute_tpi": ("terrain", "compute_tpi"),
    "get_device": ("ml.device", "get_device"),
    "log": ("io", "log"),
    "parse_kml": ("validate", "parse_kml"),
    "read_dem": ("io", "read_dem"),
    "render_folium_map": ("mapping", "render_folium_map"),
    "run_inference": ("ml.predict", "run_inference"),
    "run_training": ("ml.train", "run_training"),
    "score_routes": ("vectors", "score_routes"),
    "validate_run": ("validate", "validate_run"),
    "write_raster": ("io", "write_raster"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = __import__(f"{__name__}.{module_name}", fromlist=[attr_name])
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
