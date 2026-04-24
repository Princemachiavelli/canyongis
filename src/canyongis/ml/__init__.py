"""ML canyon/drainage detector.

Replaces the rule-based detection in masks.py with a supervised U-Net trained
on DEM-derived feature stacks and KML polyline labels. Terrain and hydrology
modules remain and serve only as feature extractors.
"""

from __future__ import annotations

from .config import MLConfig
from .device import amp_enabled, get_device

__all__ = ["MLConfig", "amp_enabled", "get_device"]
