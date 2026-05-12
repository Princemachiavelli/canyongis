from __future__ import annotations

import numpy as np
from rasterio.transform import from_origin

from canyongis.ml.labels import soft_label_from_mask


def test_soft_label_peak_on_line():
    mask = np.zeros((64, 64), dtype=np.uint8)
    mask[32, :] = 1  # horizontal line
    y = soft_label_from_mask(mask, pixel_size=1.0, sigma_m=3.0, floor=0.05)
    # On-line pixels should have value ~1.0.
    assert y[32, 32] > 0.99
    # Falloff at distance = sigma should be ~exp(-0.5) ≈ 0.607.
    assert 0.5 < y[32 + 3, 32] < 0.7
    # Floor cuts the tails.
    far = y[32 + 20, 32]
    assert far == 0.0


def test_soft_label_empty_mask_returns_zeros():
    mask = np.zeros((16, 16), dtype=np.uint8)
    y = soft_label_from_mask(mask, pixel_size=1.0, sigma_m=5.0, floor=0.05)
    assert y.shape == mask.shape
    assert y.dtype == np.float32
    assert (y == 0).all()


def test_soft_label_pixel_size_affects_falloff():
    mask = np.zeros((64, 64), dtype=np.uint8)
    mask[32, :] = 1
    y_fine = soft_label_from_mask(mask, pixel_size=1.0, sigma_m=3.0, floor=0.0)
    y_coarse = soft_label_from_mask(mask, pixel_size=3.0, sigma_m=3.0, floor=0.0)
    # With coarser pixel, sigma_m = pixel_size -> sigma_px = 1 -> tighter falloff.
    assert y_coarse[35, 32] < y_fine[35, 32]


def test_build_labels_from_kml_integration(tmp_path):
    """End-to-end: write a small KML with a descent line and a rappel point; rasterize."""
    import rasterio
    from shapely.geometry import LineString

    from canyongis.ml.config import MLConfig
    from canyongis.ml.labels import build_labels_from_kml

    # UTM-ish projected grid, 1 m per pixel.
    transform = from_origin(500000, 4100000, 1.0, 1.0)
    crs = rasterio.crs.CRS.from_epsg(26911)
    shape = (64, 64)

    # Descent line across the middle of the tile (in the projected CRS).
    # Convert to WGS84 for the KML.
    from pyproj import Transformer

    t = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    line_proj = LineString(
        [(500010, 4099968), (500050, 4099968)]
    )  # horizontal line at y = 4099968 → row 32
    line_wgs = LineString([t.transform(x, y) for x, y in line_proj.coords])
    rappel_proj = (500020, 4099968)
    rappel_wgs = t.transform(*rappel_proj)

    kml_path = tmp_path / "synth.kml"
    coords = " ".join(f"{x},{y},0" for x, y in line_wgs.coords)
    kml_path.write_text(
        '<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        f"<Placemark><name>Full Descent</name><LineString><coordinates>{coords}</coordinates></LineString></Placemark>"
        f"<Placemark><name>Rappel 1</name><Point><coordinates>{rappel_wgs[0]},{rappel_wgs[1]},0</coordinates></Point></Placemark>"
        "</Document></kml>"
    )

    cfg = MLConfig()
    canyon, rappel = build_labels_from_kml(kml_path, transform, crs, shape, 1.0, cfg)
    assert canyon.shape == shape
    assert rappel.shape == shape
    # The line should produce at least some positive pixels.
    assert canyon.max() > 0.9
    # The rappel point should produce at least one positive pixel.
    assert rappel.max() > 0.9
