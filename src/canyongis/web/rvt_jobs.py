"""Register the RVT-py job spec."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..rvt_preview import RVT_VIS_KEYS, RVT_VIS_LABELS, export_rvt_previews
from .dem_jobs import DemJobSpec, OutputUrlFn, ProgressCb, Visualization, _tile_payload, register


class RvtRequest(BaseModel):
    visualizations: Sequence[str] = Field(default=RVT_VIS_KEYS)
    display_pct: tuple[float, float] = (2.0, 98.0)
    alpha_gamma: float = 0.7


def _run_rvt(
    *,
    dem_path: str,
    out_dir: Path,
    req: RvtRequest,
    progress_cb: ProgressCb,
    output_url: OutputUrlFn,
) -> dict[str, Any]:
    artifacts = export_rvt_previews(
        dem_path,
        out_dir,
        visualizations=tuple(req.visualizations),
        display_pct=req.display_pct,
        alpha_gamma=req.alpha_gamma,
        log_callback=progress_cb,
    )
    result: dict[str, Any] = {
        "dem_path": dem_path,
        "out_dir": str(out_dir),
        "produced_visualizations": list(artifacts.keys()),
    }
    for key, art in artifacts.items():
        result[f"rvt_{key}_path"] = art.tif_path
        result[f"panel_rvt_{key}_url"] = output_url(Path(art.panel_path))
        result.update(_tile_payload(f"panel_rvt_{key}", art.tile_archive, output_url))
        if "bounds_wgs84" not in result:
            result["bounds_wgs84"] = art.bounds_wgs84
    return result


register(
    DemJobSpec(
        key="rvt",
        label="RVT toolbox",
        description=(
            "Relief Visualization Toolbox (Kokalj & Somrak). Produces sky-view "
            "factor, openness (positive / negative), sky illumination, local dominance, "
            "MSRM, e4MSTP, SLRM, and the VAT composite in one pass."
        ),
        request_model=RvtRequest,
        visualizations=tuple(
            Visualization(key=k, label=RVT_VIS_LABELS[k], url_field=f"panel_rvt_{k}_url")
            for k in RVT_VIS_KEYS
        ),
        runner=_run_rvt,
    )
)
