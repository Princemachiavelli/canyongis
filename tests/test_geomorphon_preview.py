from __future__ import annotations

import numpy as np
import rasterio
from PIL import Image
from rasterio.transform import from_origin

from canyongis.geomorphon_preview import (
    FORM_COLORS,
    export_form_preview_png,
    find_form_rasters,
)


def _write_forms(path, *, width: int = 128, height: int = 128) -> None:
    forms = np.ones((height, width), dtype=np.uint8)
    forms[height // 2, width // 2] = 3
    forms[height // 2 + 1, width // 2 + 1] = 9
    forms[0, 0] = 255
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="uint8",
        crs="EPSG:26911",
        transform=from_origin(500000.0, 4100000.0, 1.0, 1.0),
        nodata=255,
    ) as dst:
        dst.write(forms, 1)


def test_export_form_preview_png_draws_corner_labels_and_metadata(tmp_path):
    forms_tif = tmp_path / "geomorphon_forms.tif"
    _write_forms(forms_tif)

    result = export_form_preview_png(forms_tif)

    out_png = forms_tif.parent / "pngs" / "geomorphon_forms_classes_with_corners.png"
    assert result.png_path == str(out_png)
    assert out_png.exists()

    with Image.open(out_png) as image:
        assert image.size == (128, 128)
        assert "center_lat" in image.info
        assert "center_lon" in image.info
        assert "nw_lat" in image.info
        assert "se_lon" in image.info
        rgb = np.asarray(image.convert("RGB"))

    assert tuple(rgb[64, 64]) == FORM_COLORS[3]
    assert tuple(rgb[65, 65]) == FORM_COLORS[9]


def test_find_form_rasters_recurses_directories(tmp_path):
    forms_tif = tmp_path / "dem.geomorphons" / "30m" / "geomorphon_forms.tif"
    forms_tif.parent.mkdir(parents=True)
    _write_forms(forms_tif, width=8, height=8)

    assert find_form_rasters([tmp_path]) == [forms_tif]
