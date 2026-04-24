# ARCHITECTURE.md

## Overview

`canyongis` analyzes high-resolution Digital Elevation Model (DEM) data to identify candidate canyoneering routes. The pipeline derives terrain and hydrology products from a projected DEM (1–10 m), combines them into masks that highlight canyon-like features, and scores potential routes by descent, grade, and cliff density. Outputs include GeoTIFF rasters, GeoPackage vectors, and an interactive Folium HTML map.

## Pipeline Stages

1. **Input & Preprocessing**
   - Read DEM (single-band, projected CRS).
   - Validate resolution and CRS; optionally resample or clip.
   - Fill depressions and resolve flats to ensure continuous flow.

2. **Terrain Analysis**
   - **Slope**: Compute local slope (degrees) to identify steep terrain.
   - **Topographic Position Index (TPI)**: Measure local elevation relative to a neighborhood to distinguish ridges, valleys, and midslope positions.
   - **Local Relief**: Calculate elevation range within a moving window to highlight areas of high vertical relief.

3. **Hydrologic Analysis**
   - **D8 Flow Accumulation**: Compute upslope contributing area using the D8 algorithm (pysheds). High accumulation indicates stream channels.
   - **Height Above Nearest Drainage (HAND)**: Estimate vertical distance to the nearest drainage cell to separate hillslopes from channels.

4. **Mask Generation**
   - **Channel Mask**: Combine flow accumulation and HAND thresholds to extract likely channel cells.
   - **Cliff/Rappel Mask**: Use slope and local relief to locate near-vertical drops suitable for rappelling.
   - **Rappel Points**: Identify discrete candidate anchor points along cliffs.

5. **Vectorization & Routing**
   - **Drainage Polygons**: Polygonize channel masks and compute geometric properties (length, sinuosity, width).
   - **Route Scoring**: Generate candidate routes (e.g., along-channel paths), scoring by:
     - Total descent (favoring deeper canyons).
     - Average grade (penalizing excessively steep or flat sections).
     - Cliff count and spacing (balancing technical interest with safety).
   - Export routes as vector features with scores and metadata.

6. **Output & Visualization**
   - **Rasters**: Slope, TPI, local relief, flow accumulation, HAND, and masks as GeoTIFFs.
   - **Vectors**: GeoPackage containing rappel points, drainage polygons, and candidate routes.
   - **Folium Map**: Interactive HTML map overlaying key layers for exploration.

## Key Algorithms & Tools

- **Slope**: Finite difference or Horn’s method (via GDAL/rasterio) to compute gradient magnitude.
- **TPI**: Difference between cell elevation and mean elevation of a circular or annular neighborhood; configurable radius.
- **Local Relief**: Max–min elevation within a moving window (e.g., 3×3 to 15×15 cells).
- **D8 Flow Accumulation**: pysheds implementation; each cell flows to its steepest downslope neighbor; accumulation counts contributing upslope cells.
- **HAND**: For each cell, compute elevation difference to the nearest downstream cell identified as part of a drainage network (often via thresholded flow accumulation).
- **Depression Filling & Flat Resolution**: Preprocessing steps to ensure continuous flow paths (commonly via priority-flood or similar).
- **Mask Thresholding**: Empirical or configurable thresholds on slope, TPI, flow accumulation, and HAND to isolate channels and cliffs.
- **Polygonization & Vector Analysis**: GDAL polygonization of binary masks; geometric and network-based scoring of routes.

## Configuration

All tunables are exposed via `Config` in `config.py` and CLI flags, including:
- Slope and TPI neighborhood sizes.
- Flow accumulation and HAND thresholds for channel detection.
- Slope and relief thresholds for cliff/rappel masks.
- Route scoring weights (descent, grade, cliff count).
- Output format options (e.g., disable Folium map).

## Dependencies

- **GDAL/rasterio**: DEM I/O and raster algebra.
- **pysheds**: D8 flow accumulation and related hydrologic routines.
- **NumPy/SciPy**: Array operations and neighborhood statistics.
- **GeoPandas/Shapely**: Vectorization and geometric analysis.
- **Folium**: Interactive map rendering.
- **Nix**: Reproducible environment and packaging.

## Extensibility

- Additional terrain indices (e.g., curvature, aspect) can be added in `terrain.py`.
- Alternative flow algorithms (D∞, MFD) can be integrated via pysheds or similar.
- Custom mask combinations and scoring functions can be implemented in `masks.py` and `vectors.py`.
- New output formats (e.g., vector tiles) can be added in `io.py` and `mapping.py`.