from pathlib import Path

from canyongis.ml.manifest_mosaic import _write_kml_mosaic

dem_dir = Path("data/ropewiki/dem")
EXCLUDE = {"USGS_one_meter_x32y414_UT_ZionNP_QL2_2016.tif"}
ut_sources = sorted(
    p for p in dem_dir.glob("*.tif")
    if ("_12_" in p.name or "x32y41" in p.name or "x33y41" in p.name)
    and p.name not in EXCLUDE
)
ut_sources = [p for p in ut_sources if p.parent == dem_dir]
print("UT sources:", [p.name for p in ut_sources])

out = Path("data/ropewiki/dem/mosaic/Englestead_Hollow_descent_buffer2000m.tif")
n, shape = _write_kml_mosaic(
    "tmp/Englestead_Hollow_descent_only.kml",
    ut_sources,
    out,
    buffer_m=2000.0,
    max_pixels=120_000_000,
)
print(f"wrote {out} ({shape[1]}x{shape[0]}, sources={n})")
