# MIGRATE_FROM_HILLSHADE_TO_RGBA_OVERLAY

Author: opencode
Started: 2026-05-14

## Goal

All feature preview panels (flow, stream, TPI, geomorphon, curvature, RVT scores)
should output RGBA WebP/PMTiles with transparency instead of being statically
blended onto the hillshade backdrop.  The WebUI can then layer hillshade
independently as a base layer and overlay any feature on top, letting the user
toggle hillshade on/off.

## Done

### `src/canyongis/ravine_compare.py`

| Change | Status |
|--------|--------|
| `_render_rgba_panel()` — colorizes score, derives alpha from score intensity via `alpha_gamma` | ✅ |
| `_blend_over_hillshade()` removed | ✅ |
| `_render_score_panel()` removed | ✅ |
| `_warp_rgb_image_to_wgs84()` handles RGBA (nearest-neighbour for alpha band) | ✅ |
| `export_ravine_compare()` uses `a_rgba/b_rgba/c_rgba` | ✅ |
| `export_geomorphon_ravine_preview()`: `compute_hillshade` removed, uses `_render_rgba_panel` | ✅ |
| `export_tpi_ravine_preview()`: `compute_hillshade` removed, uses `_render_rgba_panel` | ✅ |
| `export_curvature_preview()`: `compute_hillshade` removed, uses `_render_rgba_panel` | ✅ |
| `export_hillshade_preview()` left as RGB (the hillshade **is** the backdrop) | ✅ |

### `src/canyongis/drainage_preview.py`

| Change | Status |
|--------|--------|
| Flow accumulation: outputs RGBA (`_colorize_score` + alpha power), no hillshade | ✅ |
| Stream network: outputs RGBA with transparent background (alpha=255 on stream pixels) | ✅ |
| `compute_hillshade` import/call removed | ✅ |
| `_blend_over_hillshade` import removed | ✅ |

### `src/canyongis/rvt_preview.py`

| Change | Status |
|--------|--------|
| Uses `_render_rgba_panel` instead of `_render_score_panel` | ✅ |
| `compute_hillshade` import/call removed | ✅ |
| `_render_score_panel` import removed | ✅ |

## Remaining

### 1. Fix ruff lint errors

- **`ravine_compare.py`**: 10× `RUF059` — `pixel_size` unpacked but unused in
  `export_geomorphon_ravine_preview`.  Already renamed one instance to
  `_pixel_size` but ruff still finds more (likely from `compute_a_geomorphon`
  not needing the value in the caller).
- **`rvt_preview.py`** / **`drainage_preview.py`**: run `ruff check --fix` and
  `ruff format` until clean.

### 2. Verify unused-import cleanup

- `ravine_compare.py` may have stale `from PIL import ImageDraw, ImageFont` etc.
- `rvt_preview.py` may have stale `import rvt.vis` (still needed for other vis).

### 3. Nix build

```bash
nix build .#canyongis
```

### 4. Test suite

```bash
python -m pytest -x -q -m "not slow"
```

### 5. WebUI verification

Leaflet's `ImageOverlay` natively handles RGBA — no JS change required.
But verify in the browser that:

- The panel tiles have proper transparency (not black where nodata).
- Hillshade layer sits below the overlay layer.
- No double-hillshade effect (hillshade should only appear once).

### 6. (Future) Port `export_ravine_compare` (the 2×2 grid panel)

The `export_ravine_compare` function creates a 2×2 composite with hillshade +
A/B/C panels.  This function is only used by the CLI
`canyongis-ravine-compare`, not the web server — lower priority.
