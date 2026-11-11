import numpy as np
from PIL import Image

from canyongis.ravine_compare import _overlay_kml_mask


def _mask() -> np.ndarray:
    m = np.zeros((9, 9), dtype=bool)
    m[4, :] = True
    return m


def test_overlay_kml_mask_rgb():
    img = Image.fromarray(np.zeros((9, 9, 3), dtype=np.uint8), mode="RGB")
    _overlay_kml_mask(img, _mask(), color=(255, 0, 255), width_px=1)
    a = np.asarray(img)
    assert tuple(a[4, 4]) == (255, 0, 255)
    assert tuple(a[0, 0]) == (0, 0, 0)


def test_overlay_kml_mask_rgba_raises_alpha_on_line():
    img = Image.fromarray(np.zeros((9, 9, 4), dtype=np.uint8), mode="RGBA")
    _overlay_kml_mask(img, _mask(), color=(255, 0, 255), width_px=1, opacity=1.0)
    a = np.asarray(img)
    assert img.mode == "RGBA"
    assert tuple(a[4, 4]) == (255, 0, 255, 255)
    assert tuple(a[0, 0]) == (0, 0, 0, 0)
