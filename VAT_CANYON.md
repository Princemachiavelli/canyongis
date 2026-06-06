# Canyon VAT Notes

`src/canyongis/rvt_preview.py` currently implements a canyon-oriented variant of RVT-py's combined VAT for the `vat` preview layer.

## RVT Baseline

RVT VAT is a four-layer blend:

- Hillshade, normal blend, used as the base image.
- Slope gradient, luminosity blend, 50% opacity.
- Positive openness, overlay blend, 50% opacity.
- Sky-view factor, multiply blend, 25% opacity.

RVT-py's documented combined VAT blends VAT computed with general terrain settings and VAT computed with flat terrain settings. Flat-terrain VAT improves subtle low-relief features, but RVT's own documentation warns that it can oversaturate very steep slopes.

## Current Implementation

The current implementation renders three VAT passes and blends them by slope:

- `flat`: RVT flat-terrain VAT.
- `general`: RVT general-terrain VAT.
- `steep`: RVT steep-terrain VAT-style settings.

Flat blend:

- `VAT_FLAT_FULL_SLOPE_DEG = 4.0`
- `VAT_FLAT_ZERO_SLOPE_DEG = 12.0`
- Flat VAT is full strength at <= 4 degrees and fades to zero by 12 degrees.

Steep blend:

- `VAT_STEEP_BLEND_START_DEG = 35.0`
- `VAT_STEEP_BLEND_END_DEG = 55.0`
- General VAT fades toward the steep VAT pass from 35 to 55 degrees.

General VAT settings:

- Slope stretch: `0-50`
- SVF stretch: `0.7-1.0`
- Positive openness stretch: `68-93`
- Hillshade sun elevation: `35`
- SVF radius: `10`
- SVF noise removal: `0`

Flat VAT settings:

- Slope stretch: `0-15`
- SVF stretch: `0.9-1.0`
- Positive openness stretch: `85-93`
- Hillshade sun elevation: `15`
- SVF radius: `20`
- SVF noise removal: `3`

Steep VAT settings:

- Slope stretch: `0-60`
- SVF stretch: `0.55-1.0`
- Positive openness stretch: `55-95`
- Hillshade sun elevation: `55`
- SVF radius: `10`
- SVF noise removal: `0`

## Observed Issues

- The steep lower canyon areas remain heavily oversaturated/crushed, despite slope-based blending and several hillshade/SVF experiments.
- Areas above roughly 70 degrees can collapse toward near-black.
- Moderately steep terrain around 40 degrees can become undersaturated when the steep preset is applied too broadly.
- The best observed steep-area result was an earlier steep-terrain preset, but it made less-steep upper terrain slightly worse.
- Reducing hillshade opacity alone did not help because RVT's blender uses the bottom hillshade layer as the initial background; opacity on that base layer is effectively ignored.
- Pre-compressing hillshade contrast and reducing SVF opacity also did not materially improve the steep-area oversaturation.

## Likely Causes

The problem is probably not only slope normalization. The failed slope-only changes suggest the crushing is driven by the interaction of:

- Hillshade as the base layer, especially for canyon wall aspects facing away from the light azimuth.
- Sky-view factor multiply darkening deeply incised terrain.
- Positive openness overlay amplifying local contrast in enclosed terrain.

## Next Directions

Future experiments should probably avoid more global slope stretch changes and instead inspect intermediate rasters for a representative canyon:

- Save/debug `hillshade`, `slope`, `positive openness`, and `SVF` normalized layers for the same viewport.
- Compare the lower steep half against the upper moderate half before blending.
- Try a canyon-specific composite that does not use single-azimuth hillshade as the base, e.g. multi-directional hillshade or sky illumination.
- Consider using general VAT only for the base map and separate analytical overlays for canyon wall structure.
- If retaining VAT, test slope-dependent reductions of SVF multiply and openness overlay, not just slope-dependent selection of full VAT presets.
