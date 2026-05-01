from pathlib import Path

from canyongis.validate import parse_kml

src = Path("data/ropewiki/kml/Englestead_Hollow.kml")
dst = Path("tmp/Englestead_Hollow_descent_only.kml")

features = parse_kml(src)
descent = features.descent_lines[features.descent_lines["name"] == "Englestead - Descent"]
assert len(descent) == 1, f"expected one Englestead descent line, found {len(descent)}"
geom = descent.geometry.iloc[0]
coords = " ".join(f"{x},{y}" for x, y in geom.coords)
dst.write_text(
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    "<Placemark><name>Englestead - Descent</name>"
    f"<LineString><coordinates>{coords}</coordinates></LineString>"
    "</Placemark></Document></kml>\n"
)
print("wrote", dst, "with", len(geom.coords), "points; bounds", geom.bounds)
