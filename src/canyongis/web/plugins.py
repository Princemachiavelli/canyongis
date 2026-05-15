from __future__ import annotations

from . import source_jobs as _source_jobs  # noqa: F401
from .dem_jobs import (
    DemJobSpec,
    InputSpec,
    LayerSpec,
    OutputSpec,
    Visualization,
    get_spec,
    list_specs,
    normalize_result,
    register,
)

ToolSpec = DemJobSpec

__all__ = [
    "InputSpec",
    "LayerSpec",
    "OutputSpec",
    "ToolSpec",
    "Visualization",
    "get_spec",
    "list_specs",
    "normalize_result",
    "register",
]
