Recommendation up front: the trick that maps cleanest to your "drop within R" approach is **multi-scale subtraction** — compute drop at two radii and subtract, or use the morphological **black-hat transform** (closing minus original, or here drop@small minus drop@large). Both isolate features *smaller* than your structuring element while suppressing broad walls.

**Why your current map saturates**

`drop_to_lower_ground@R` is a morphological erosion (local-min) subtracted from the surface. Above a critical R, every pixel near a tall wall reports the full wall height; the small ledge inside is invisible. You need either band-pass behavior or a measure that responds to *curvature* (rate of slope change) rather than relief.

**Techniques that highlight small features inside large ones**

1. **Top-hat / Black-hat morphology** — `DEM − opening(DEM, R)` keeps bumps smaller than R; `closing(DEM, R) − DEM` keeps pits/notches smaller than R. Directly subtracts out the wall's smooth shape and leaves the ledge.

2. **Multi-scale residual / Local Relief Model (LRM)** — `DEM − smoothed(DEM, R)`. Same idea with a Gaussian or mean filter. Used heavily in lidar archaeology for finding sub-meter features under terrain.

3. **Profile curvature** — second derivative along the fall-line. Cliff lips and ledges have huge negative profile curvature; uniform steep walls don't. Pair with a slope mask to keep only "steep AND high curvature" (= cliff lip).

4. **Topographic Position Index (TPI)** — `DEM − mean(DEM, R)`. Andrew Weiss's classic; multi-scale TPI (small R + large R combined) classifies landforms (ridge, slope, channel, ledge, etc.).

5. **Multiscale Standard Deviation of Normals (MSDN)** — Lindsay/Newman: how much surface-normal vectors vary in a neighborhood. Cliffs spike. Excellent edge detector for terrain.

6. **Slope-of-slope / Laplacian-of-Gaussian** — second derivative of elevation at scale σ. Pure linear analogue of the morphological top-hat.

7. **Vertical drop in a *very small* annulus** — instead of a disk min, take the min over a thin ring at radius R±ε. This responds to the *change* across distance R, not the cumulative drop, so a 20m wall with a 3m ledge inside reads "3" at R=3 and "0" elsewhere — peak shifts toward feature size.

8. **Geomorphons at fine search radius** — already in your toolkit. Smaller `search_m` picks up sub-cliff features (slope/spur/footslope cells inside the wall).

**Tools that bundle these well**

- **WhiteboxTools** (Lindsay; `whitebox` Python package, MIT-licensed, very fast): `DeviationFromMeanElevation`, `MaxDifferenceFromMean`, `MultiscaleTopographicPositionImage`, `MultiscaleStdDevNormals`, `Geomorphons`, `ProfileCurvature`, `RuggednessIndex`. Probably the best fit for your workflow — it's just a CLI/Python lib, no GRASS session needed.
- **GRASS GIS**: `r.geomorphon`, `r.tpi`, `r.param.scale` (Wood's multi-scale curvature classification — exactly the "small features inside large" problem), `r.relief.scaled`, `r.slope.aspect` (profile/plan/tangential curvature), `r.mfilter` for custom kernels.
- **SAGA**: `Topographic Position Index (multi-scale)`, `Convergence Index`, `Vertical Distance to Channel Network`, `Mass Balance Index`.
- **RichDEM** (Python): curvature, TPI, depression analysis.

If you want a quick experiment in your existing pipeline, the cheapest win is probably to render `drop@5m − 0.5·drop@20m` (a band-pass on what you already compute) or add a black-hat preview alongside.
