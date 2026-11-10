"""Offline topographic map layers for a canyon GeoPackage, rendered from open data.

Three tile layers come out of this module (all EPSG:3857, read by the viewer like any other):

* ``tiles_osm``: an opaque base map. Hillshade relief from the 1 m DEM, forest shading from
  the NAIP near-infrared band (NDVI) plus OSM forest polygons, water, rock and scrub areas,
  streams, roads, tracks and trails, and peak / spring / camp / parking symbols with names.
* ``tiles_contours_m`` and ``tiles_contours_ft``: transparent contour-line overlays with
  elevation labels on the index contours, one per unit so the labels follow the viewer's
  metres / feet setting (labels baked into raster tiles cannot be converted afterwards).

OSM data comes from the Overpass API (cached next to the other downloads) and is read with
GDAL's OSM driver. Contours are built from the DEM after smoothing it to about 5 m.

Rendering uses Pillow only. Every tile is drawn on a canvas with a margin around it and
cropped, so lines and labels continue across tile edges; label positions are decided once per
zoom level for the whole area, so neighbouring tiles agree on them.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tiles import TILE_SIZE, WORLD_HALF_SIZE, tile_bounds_3857

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_ATTEMPTS = 2  # per mirror
_USER_AGENT = "RopeGeo/1.0"
SUPERSAMPLE = 2
MARGIN_PX = 48  # extra canvas around a tile, in output pixels
FEET_PER_METRE = 3.280839895

# Contour spacing (and which contours are labelled "index" contours), in the layer's unit.
CONTOUR_STEPS = {"m": (10.0, 50.0), "ft": (40.0, 200.0)}
# Zoom from which the thin (non-index) contours are drawn.
MINOR_CONTOURS_FROM_ZOOM = 16

BACKGROUND = (243, 240, 232)
FOREST = (146, 188, 120)
WATER = (165, 191, 221)
ROCK = (216, 208, 196)
SCRUB = (206, 222, 184)
ROAD_CASING = (150, 130, 90)
ROAD_MAJOR = (247, 196, 98)
ROAD_MINOR = (255, 255, 255)
TRACK = (139, 90, 43)
PATH = (176, 48, 26)
STREAM = (95, 140, 200)
CONTOUR_MINOR = (150, 100, 55)
CONTOUR_INDEX = (120, 70, 30)
LABEL = (55, 45, 35)
HALO = (255, 255, 255)

_HIGHWAY_KINDS = {
    "motorway": "road_major",
    "trunk": "road_major",
    "primary": "road_major",
    "secondary": "road_major",
    "tertiary": "road_major",
    "motorway_link": "road_major",
    "trunk_link": "road_major",
    "primary_link": "road_major",
    "secondary_link": "road_major",
    "unclassified": "road_minor",
    "residential": "road_minor",
    "service": "road_minor",
    "living_street": "road_minor",
    "track": "track",
    "path": "path",
    "footway": "path",
    "bridleway": "path",
    "steps": "path",
    "cycleway": "path",
    "pedestrian": "path",
}


# ---- OSM data ---------------------------------------------------------------------------------


@dataclass
class OsmData:
    """OSM features in Web Mercator metres."""

    lines: list[tuple[Any, str, str]] = field(default_factory=list)  # (geometry, kind, name)
    areas: list[tuple[Any, str]] = field(default_factory=list)  # (geometry, kind)
    points: list[tuple[float, float, str, str, str]] = field(
        default_factory=list
    )  # x, y, kind, name, ele


def overpass_query(bbox_lonlat: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox_lonlat
    box = f"{south},{west},{north},{east}"
    return f"""[out:xml][timeout:120];
(
  way["highway"]({box});
  way["waterway"~"^(river|stream|canal|drain)$"]({box});
  way["natural"~"^(water|wood|scrub|bare_rock|scree|cliff)$"]({box});
  way["landuse"~"^(forest|reservoir)$"]({box});
  relation["natural"~"^(water|wood|scrub|bare_rock|scree)$"]({box});
  relation["landuse"~"^(forest|reservoir)$"]({box});
  node["natural"~"^(peak|spring|saddle|cave_entrance)$"]({box});
  node["tourism"~"^(camp_site|viewpoint|alpine_hut|picnic_site)$"]({box});
  node["amenity"="parking"]({box});
);
(._;>;);
out body;
"""


def fetch_osm(bbox_lonlat: tuple[float, float, float, float], cache_dir: Path) -> Path:
    """Download OSM data for the bbox (Overpass) to ``cache_dir/osm.xml``; reused if present."""
    out = cache_dir / "osm.xml"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    body = urllib.parse.urlencode({"data": overpass_query(bbox_lonlat)}).encode()
    errors: list[str] = []
    data = b""
    for url in OVERPASS_URLS:
        for attempt in range(OVERPASS_ATTEMPTS):
            req = urllib.request.Request(url, data=body, headers={"User-Agent": _USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=300) as resp:
                    data = resp.read()
            except (urllib.error.URLError, TimeoutError) as exc:  # 429/502/504 are busy servers
                errors.append(f"{url}: {exc}")
                time.sleep(5 * (attempt + 1))
                continue
            if b"<osm" in data[:2000]:
                break
            errors.append(f"{url}: {data[:200].decode('utf-8', 'replace')}")
            data = b""
        if data:
            break
    if not data:
        raise RuntimeError("Overpass is not answering:\n  " + "\n  ".join(errors))
    tmp = out.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(out)
    return out


_TAG = re.compile(r'"([^"]+)"=>"((?:[^"\\]|\\.)*)"')


def parse_other_tags(text: object) -> dict[str, str]:
    """GDAL's OSM driver packs tags it has no column for into an hstore-style string."""
    return {k: v.replace('\\"', '"') for k, v in _TAG.findall(_text(text))}


def _text(value: Any) -> str:
    """A column value as text; missing values come back from GDAL as None or NaN."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value)


_OSM_CONFIG: str | None = None


def _osm_config() -> str | None:
    """Path of a GDAL OSM driver config that keeps ``ele`` (the default one drops it).

    Written once to a temp file from GDAL's own osmconf.ini; None if that cannot be found.
    """
    global _OSM_CONFIG
    if _OSM_CONFIG is None:
        import tempfile

        from osgeo import gdal

        default = gdal.FindFile("gdal", "osmconf.ini")
        if not default:
            return None
        text = Path(default).read_text()
        text = text.replace(
            "unsignificant=created_by,converted_by,source,time,ele,attribution",
            "unsignificant=created_by,converted_by,source,time,attribution",
        )
        text = re.sub(r"(\[points\][^\[]*?attributes=name)", r"\1,ele", text, count=1, flags=re.S)
        handle = tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False)
        handle.write(text)
        handle.close()
        _OSM_CONFIG = handle.name
    return _OSM_CONFIG


def _area_kind(row: dict[str, Any], tags: dict[str, str]) -> str | None:
    natural = _text(row.get("natural")) or tags.get("natural")
    landuse = _text(row.get("landuse")) or tags.get("landuse")
    if natural == "water" or landuse == "reservoir":
        return "water"
    if natural == "wood" or landuse == "forest":
        return "forest"
    if natural in {"bare_rock", "scree"}:
        return "rock"
    if natural == "scrub":
        return "scrub"
    return None


def load_osm(path: Path) -> OsmData:
    """Read an OSM XML/PBF file into ``OsmData`` (Web Mercator)."""
    import pyogrio
    from shapely import force_2d

    config = _osm_config()

    def frame(layer: str):
        options = {"CONFIG_FILE": config} if config else {}
        try:
            gdf = pyogrio.read_dataframe(path, layer=layer, **options)
        except Exception:
            return None
        if gdf is None or gdf.empty:
            return None
        return gdf.set_crs(4326, allow_override=True).to_crs(3857)

    data = OsmData()
    lines = frame("lines")
    if lines is not None:
        for row in lines.to_dict("records"):
            tags = parse_other_tags(row.get("other_tags"))
            geom = row["geometry"]
            if geom is None:
                continue
            if _text(row.get("highway")):
                kind = _HIGHWAY_KINDS.get(row["highway"])
            elif _text(row.get("waterway")) in {"river", "stream", "canal", "drain"}:
                kind = "stream"
            elif tags.get("natural") == "cliff":
                kind = "cliff"
            else:
                kind = None
            if kind:
                data.lines.append((force_2d(geom), kind, _text(row.get("name"))))
    areas = frame("multipolygons")
    if areas is not None:
        for row in areas.to_dict("records"):
            tags = parse_other_tags(row.get("other_tags"))
            kind = _area_kind(row, tags)
            if kind and row["geometry"] is not None:
                data.areas.append((force_2d(row["geometry"]), kind))
    points = frame("points")
    if points is not None:
        for row in points.to_dict("records"):
            tags = parse_other_tags(row.get("other_tags"))
            natural, tourism = tags.get("natural"), tags.get("tourism")
            amenity = tags.get("amenity")
            kind = (
                natural
                if natural in {"peak", "spring", "saddle", "cave_entrance"}
                else "camp"
                if tourism in {"camp_site", "alpine_hut"}
                else "viewpoint"
                if tourism in {"viewpoint", "picnic_site"}
                else "parking"
                if amenity == "parking"
                else None
            )
            geom = row["geometry"]
            if kind and geom is not None and not geom.is_empty:
                ele = _text(row.get("ele")) or tags.get("ele", "")
                data.points.append((geom.x, geom.y, kind, _text(row.get("name")), ele))
    return data


# ---- contours and vegetation --------------------------------------------------------------------


def build_contours(dem_path: Path, unit: str) -> list[tuple[Any, float, bool]]:
    """Contour lines from a DEM as ``(LineString in EPSG:3857, elevation in unit, is_index)``.

    The DEM is averaged down to about 5 m and lightly smoothed first, so 1 m lidar noise does
    not turn into a tangle of lines. ``unit`` is "m" or "ft".
    """
    import numpy as np
    import rasterio
    from osgeo import gdal, ogr, osr
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from scipy.ndimage import gaussian_filter
    from shapely import wkb
    from shapely.ops import transform as shp_transform

    interval, index_every = CONTOUR_STEPS[unit]
    with rasterio.open(dem_path) as src:
        factor = max(1, round(5.0 / abs(src.res[0])))
        out_shape = (max(2, src.height // factor), max(2, src.width // factor))
        elev = src.read(1, out_shape=out_shape, resampling=Resampling.average, masked=True)
        transform = src.transform * src.transform.scale(
            src.width / out_shape[1], src.height / out_shape[0]
        )
        crs = src.crs
    valid = ~np.ma.getmaskarray(elev)
    if not valid.any():
        return []
    values = np.ma.filled(elev, np.nan).astype("float64")
    fill = float(np.nanmean(values))
    smooth = gaussian_filter(np.where(valid, values, fill), sigma=1.0)
    if unit == "ft":
        smooth = smooth * FEET_PER_METRE
    nodata = -9999.0
    smooth = np.where(valid, smooth, nodata).astype("float32")

    gdal.UseExceptions()
    mem = gdal.GetDriverByName("MEM").Create("", out_shape[1], out_shape[0], 1, gdal.GDT_Float32)
    mem.SetGeoTransform(transform.to_gdal())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(crs.to_wkt())
    mem.SetProjection(srs.ExportToWkt())
    band = mem.GetRasterBand(1)
    band.WriteArray(smooth)
    band.SetNoDataValue(nodata)

    ds = ogr.GetDriverByName("MEM").CreateDataSource("contours")
    layer = ds.CreateLayer("contour", srs=srs, geom_type=ogr.wkbLineString)
    layer.CreateField(ogr.FieldDefn("ELEV", ogr.OFTReal))
    gdal.ContourGenerateEx(
        band, layer, options=[f"LEVEL_INTERVAL={interval}", "LEVEL_BASE=0", "ELEV_FIELD=0"]
    )
    to_3857 = Transformer.from_crs(crs, 3857, always_xy=True).transform
    out: list[tuple[Any, float, bool]] = []
    for feature in layer:
        geom = feature.GetGeometryRef()
        if geom is None:
            continue
        line = wkb.loads(bytes(geom.ExportToWkb()))
        level = float(feature.GetField("ELEV"))
        is_index = abs(level / index_every - round(level / index_every)) < 1e-6
        out.append((shp_transform(to_3857, line).simplify(1.0), level, is_index))
    return out


def build_vegetation(imagery_path: Path, out_path: Path) -> Path | None:
    """Green-cover GeoTIFF (uint8, 0-255) from the near-infrared band of NAIP, ~1.2 m cells.

    Returns None when the imagery has no 4th (near-infrared) band.
    """
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from scipy.ndimage import gaussian_filter

    with rasterio.open(imagery_path) as src:
        if src.count < 4:
            return None
        factor = max(1, round(1.2 / abs(src.res[0])))
        shape = (max(2, src.height // factor), max(2, src.width // factor))
        red = src.read(1, out_shape=shape, resampling=Resampling.average).astype("float32")
        nir = src.read(4, out_shape=shape, resampling=Resampling.average).astype("float32")
        profile = src.profile.copy()
        transform = src.transform * src.transform.scale(src.width / shape[1], src.height / shape[0])
    ndvi = (nir - red) / np.maximum(nir + red, 1.0)
    # NDVI below ~0.15 is bare ground, rock or water; above ~0.45 is dense canopy.
    cover = np.clip((gaussian_filter(ndvi, 1.0) - 0.15) / 0.30, 0.0, 1.0)
    profile.update(
        count=1,
        dtype="uint8",
        height=shape[0],
        width=shape[1],
        transform=transform,
        compress="deflate",
        nodata=None,
        tiled=False,
        driver="GTiff",
    )
    profile.pop("blockxsize", None)
    profile.pop("blockysize", None)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write((cover * 255).astype("uint8"), 1)
    return out_path


# ---- drawing helpers ------------------------------------------------------------------------------


def metres_per_pixel(zoom: int) -> float:
    return 2 * WORLD_HALF_SIZE / (TILE_SIZE * (1 << zoom))


def _line_width(kind: str, zoom: int) -> float:
    """Line width in output pixels."""
    scale = 2 ** ((zoom - 15) / 2.0)
    base = {
        "road_major": 3.2,
        "road_minor": 2.2,
        "track": 1.6,
        "path": 1.3,
        "stream": 1.3,
        "cliff": 1.0,
    }[kind]
    return max(0.8, base * scale)


def _dashed(points: list[tuple[float, float]], dash: float, gap: float):
    """Split a polyline into dash segments (each a list of points)."""
    out, cur, drawing, left = [], [points[0]], True, dash
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while seg - pos > left:
            pos += left
            t = pos / seg
            pt = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            if drawing:
                cur.append(pt)
                out.append(cur)
                cur = []
            else:
                cur = [pt]
            drawing = not drawing
            left = dash if drawing else gap
        left -= seg - pos
        if drawing:
            cur.append((x1, y1))
    if drawing and len(cur) > 1:
        out.append(cur)
    return out


def _font(size: float):
    from PIL import ImageFont

    return ImageFont.load_default(size=max(6, round(size)))


@dataclass(frozen=True)
class Label:
    text: str
    x: float  # Web Mercator metres
    y: float
    angle: float  # degrees, counter-clockwise, text reads left to right
    size: float  # output pixels
    kind: str
    w: float  # output-pixel extent of its bounding box, for collisions
    h: float


def _text_extent(text: str, size: float) -> tuple[float, float]:
    left, top, right, bottom = _font(size).getbbox(text)
    return right - left + 4, bottom - top + 4


def _along(line, spacing: float, first: float) -> list[float]:
    d, out = first, []
    while d < line.length:
        out.append(d)
        d += spacing
    return out


def contour_labels(contours, zoom: int) -> list[Label]:
    """Index-contour elevation labels placed along the straighter stretches, at one zoom."""
    res = metres_per_pixel(zoom)
    size = 10.0
    labels: list[Label] = []
    for line, level, is_index in contours:
        if not is_index or line.length < 80 * res:
            continue
        text = f"{round(level):d}"
        w, h = _text_extent(text, size)
        half = (w / 2 + 2) * res
        # A stable offset per line so the pattern does not line up across neighbouring contours.
        seed = (
            round(level) * 31
            + round(line.coords[0][0] / 7) * 17
            + round(line.coords[0][1] / 7) * 13
        )
        first = (abs(seed) % 300 + 120) * res
        for d in _along(line, 420 * res, first):
            if d - half < 0 or d + half > line.length:
                continue
            a, b = line.interpolate(d - half), line.interpolate(d + half)
            mid = line.interpolate(d)
            # Only where the line is nearly straight under the label.
            chord = math.hypot(b.x - a.x, b.y - a.y)
            if chord < 0.9 * 2 * half:
                continue
            angle = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
            if angle > 90:
                angle -= 180
            elif angle < -90:
                angle += 180
            labels.append(Label(text, mid.x, mid.y, angle, size, "contour", w, h))
    return labels


def _drop_colliding(labels: list[Label], zoom: int) -> list[Label]:
    """Greedy: keep labels (in the given priority order) that do not overlap one already kept."""
    res = metres_per_pixel(zoom)
    kept: list[Label] = []
    grid: dict[tuple[int, int], list[tuple[float, float, float, float]]] = {}
    cell = 64.0
    for lab in labels:
        cx, cy = lab.x / res, lab.y / res
        # Rotated boxes are approximated by their axis-aligned bounds.
        rad = math.radians(lab.angle)
        hw = (abs(lab.w * math.cos(rad)) + abs(lab.h * math.sin(rad))) / 2 + 2
        hh = (abs(lab.w * math.sin(rad)) + abs(lab.h * math.cos(rad))) / 2 + 2
        box = (cx - hw, cy - hh, cx + hw, cy + hh)
        keys = [
            (i, j)
            for i in range(int(box[0] // cell), int(box[2] // cell) + 1)
            for j in range(int(box[1] // cell), int(box[3] // cell) + 1)
        ]
        if any(
            not (box[2] < o[0] or o[2] < box[0] or box[3] < o[1] or o[3] < box[1])
            for k in keys
            for o in grid.get(k, [])
        ):
            continue
        for k in keys:
            grid.setdefault(k, []).append(box)
        kept.append(lab)
    return kept


# ---- the renderer ---------------------------------------------------------------------------------


class TopoRenderer:
    """Renders base-map and contour tiles; ``base`` / ``contours`` return PNG bytes or None."""

    def __init__(
        self,
        osm: OsmData,
        contours: dict[str, list],
        *,
        hillshade: Path | None = None,
        vegetation: Path | None = None,
    ) -> None:
        from shapely import STRtree

        self.osm = osm
        self.contours = contours
        self.hillshade = hillshade
        self.vegetation = vegetation
        self._line_tree = STRtree([g for g, _, _ in osm.lines]) if osm.lines else None
        self._area_tree = STRtree([g for g, _ in osm.areas]) if osm.areas else None
        self._contour_trees = {u: STRtree([g for g, _, _ in c]) for u, c in contours.items() if c}
        self._label_cache: dict[tuple, list[Label]] = {}
        self._rasters: dict[Path, Any] = {}

    # -- geometry plumbing --

    def _frame(self, zoom: int, x: int, y: int):
        """Canvas bounds (metres), metres per canvas pixel and the output tile's pixel offset."""
        minx, miny, maxx, maxy = tile_bounds_3857(zoom, x, y)
        res = metres_per_pixel(zoom)
        pad = MARGIN_PX * res
        size = (TILE_SIZE + 2 * MARGIN_PX) * SUPERSAMPLE
        return (minx - pad, miny - pad, maxx + pad, maxy + pad), res / SUPERSAMPLE, size

    @staticmethod
    def _to_px(coords, bounds, cres):
        return [((px - bounds[0]) / cres, (bounds[3] - py) / cres) for px, py in coords]

    @staticmethod
    def _query(tree, bounds):
        from shapely import box

        if tree is None:
            return []
        return tree.query(box(*bounds))

    def _read_raster(self, path: Path, bounds, size: int, resampling):
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
        from rasterio.warp import reproject

        src = self._rasters.get(path)
        if src is None:
            src = self._rasters[path] = rasterio.open(path)
        out = np.zeros((size, size), dtype=np.uint8)
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=from_bounds(*bounds, size, size),
            dst_crs="EPSG:3857",
            resampling=resampling,
        )
        return out

    def _labels(self, kind: str, zoom: int) -> list[Label]:
        key = (kind, zoom)
        if key in self._label_cache:
            return self._label_cache[key]
        if kind.startswith("contours_"):
            labels = _drop_colliding(
                contour_labels(self.contours[kind.split("_", 1)[1]], zoom), zoom
            )
        else:
            labels = _drop_colliding(self._map_labels(zoom), zoom)
        self._label_cache[key] = labels
        return labels

    def _map_labels(self, zoom: int) -> list[Label]:
        labels: list[Label] = []
        if zoom >= 14:
            for x, y, kind, name, ele in self.osm.points:
                if kind == "peak" or (zoom >= 15 and name):
                    text = name or ""
                    if kind == "peak" and ele:
                        with contextlib.suppress(ValueError):
                            text = f"{name} {round(float(ele.split()[0])):d}".strip()
                    if text:
                        w, h = _text_extent(text, 11.0)
                        labels.append(
                            Label(text, x, y - 9 * metres_per_pixel(zoom), 0.0, 11.0, "point", w, h)
                        )
        if zoom >= 15:
            for geom, kind, name in self.osm.lines:
                if not name or kind not in {"road_major", "road_minor", "track", "path"}:
                    continue
                parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
                for part in parts:
                    w, h = _text_extent(name, 9.0)
                    res = metres_per_pixel(zoom)
                    if part.length < w * res * 1.2:
                        continue
                    a = part.interpolate(part.length / 2 - w * res / 2)
                    b = part.interpolate(part.length / 2 + w * res / 2)
                    mid = part.interpolate(part.length / 2)
                    angle = math.degrees(math.atan2(b.y - a.y, b.x - a.x))
                    if angle > 90:
                        angle -= 180
                    elif angle < -90:
                        angle += 180
                    labels.append(Label(name, mid.x, mid.y, angle, 9.0, "line", w, h))
        return labels

    def _draw_labels(self, canvas, labels, bounds, cres) -> None:
        from PIL import Image, ImageDraw

        size_px = canvas.size[0]
        for lab in labels:
            cx, cy = (lab.x - bounds[0]) / cres, (bounds[3] - lab.y) / cres
            reach = max(lab.w, lab.h) * SUPERSAMPLE
            if cx < -reach or cy < -reach or cx > size_px + reach or cy > size_px + reach:
                continue
            font = _font(lab.size * SUPERSAMPLE)
            stroke = max(1, round(1.5 * SUPERSAMPLE))
            left, top, right, bottom = font.getbbox(lab.text, stroke_width=stroke)
            tile = Image.new("RGBA", (right - left + 6, bottom - top + 6), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text(
                (3 - left, 3 - top),
                lab.text,
                font=font,
                fill=(*LABEL, 255),
                stroke_width=stroke,
                stroke_fill=(*HALO, 230),
            )
            if lab.angle:
                tile = tile.rotate(lab.angle, expand=True, resample=Image.Resampling.BICUBIC)
            canvas.alpha_composite(
                tile, (round(cx - tile.size[0] / 2), round(cy - tile.size[1] / 2))
            )

    def _finish(self, canvas, opaque: bool) -> bytes | None:
        from PIL import Image

        small = canvas.resize((TILE_SIZE + 2 * MARGIN_PX,) * 2, Image.Resampling.LANCZOS)
        tile = small.crop((MARGIN_PX, MARGIN_PX, MARGIN_PX + TILE_SIZE, MARGIN_PX + TILE_SIZE))
        if not opaque and not tile.getchannel("A").getbbox():
            return None  # nothing drawn inside this tile
        out = io.BytesIO()
        (tile.convert("RGB") if opaque else tile).save(out, "PNG", optimize=True)
        return out.getvalue()

    # -- layers --

    def base(self, zoom: int, x: int, y: int) -> bytes | None:
        import numpy as np
        from PIL import Image, ImageDraw
        from rasterio.enums import Resampling

        bounds, cres, size = self._frame(zoom, x, y)
        rgb = np.empty((size, size, 3), dtype="float32")
        rgb[:] = BACKGROUND
        if self.hillshade:
            shade = self._read_raster(self.hillshade, bounds, size, Resampling.bilinear)
            known = shade > 0
            factor = np.where(known, 0.62 + 0.38 * np.clip(shade / 180.0, 0, 1.3), 1.0)
            rgb *= factor[..., None]
        if self.vegetation:
            cover = self._read_raster(self.vegetation, bounds, size, Resampling.bilinear) / 255.0
            alpha = (cover * 0.62)[..., None]
            rgb = rgb * (1 - alpha) + np.array(FOREST, dtype="float32") * alpha
        canvas = Image.fromarray(np.clip(rgb, 0, 255).astype("uint8"), "RGB").convert("RGBA")

        # Areas.
        area_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(area_layer)
        order = {"rock": 0, "scrub": 1, "forest": 2, "water": 3}
        fills = {
            "rock": (*ROCK, 110),
            "scrub": (*SCRUB, 120),
            "forest": (*FOREST, 0 if self.vegetation else 110),
            "water": (*WATER, 255),
        }
        hits = sorted(
            self._query(self._area_tree, bounds), key=lambda i: order[self.osm.areas[i][1]]
        )
        for i in hits:
            geom, kind = self.osm.areas[i]
            if fills[kind][3] == 0:
                continue
            for poly in getattr(geom, "geoms", [geom]):
                if poly.geom_type != "Polygon":
                    continue
                draw.polygon(self._to_px(poly.exterior.coords, bounds, cres), fill=fills[kind])
                for ring in poly.interiors:
                    draw.polygon(self._to_px(ring.coords, bounds, cres), fill=(0, 0, 0, 0))
        canvas.alpha_composite(area_layer)

        # Lines: casings first, then fills, so crossings join cleanly.
        line_hits = [self.osm.lines[i] for i in self._query(self._line_tree, bounds)]
        draw = ImageDraw.Draw(canvas)
        draw_order = ["stream", "cliff", "path", "track", "road_minor", "road_major"]
        for kind in draw_order:
            width = _line_width(kind, zoom) * SUPERSAMPLE
            for geom, k, _ in line_hits:
                if k != kind:
                    continue
                for part in getattr(geom, "geoms", [geom]):
                    pts = self._to_px(part.coords, bounds, cres)
                    if len(pts) < 2:
                        continue
                    if kind in {"road_major", "road_minor"}:
                        draw.line(
                            pts,
                            fill=ROAD_CASING,
                            width=round(width + 2 * SUPERSAMPLE),
                            joint="curve",
                        )
                        draw.line(
                            pts,
                            fill=ROAD_MAJOR if kind == "road_major" else ROAD_MINOR,
                            width=round(width),
                            joint="curve",
                        )
                    elif kind == "track":
                        for dash in _dashed(pts, 7 * SUPERSAMPLE, 3 * SUPERSAMPLE):
                            draw.line(dash, fill=TRACK, width=round(width), joint="curve")
                    elif kind == "path":
                        for dash in _dashed(pts, 4 * SUPERSAMPLE, 3 * SUPERSAMPLE):
                            draw.line(dash, fill=PATH, width=round(width), joint="curve")
                    elif kind == "stream":
                        draw.line(pts, fill=STREAM, width=round(width), joint="curve")
                    else:
                        draw.line(pts, fill=(90, 80, 70), width=round(width), joint="curve")

        # Point symbols, then labels.
        for px, py, kind, _, _ in self.osm.points:
            cx, cy = (px - bounds[0]) / cres, (bounds[3] - py) / cres
            if not -20 < cx < size + 20 or not -20 < cy < size + 20 or zoom < 14:
                continue
            r = 5 * SUPERSAMPLE
            if kind == "peak":
                draw.polygon(
                    [(cx, cy - r), (cx - r, cy + r * 0.8), (cx + r, cy + r * 0.8)],
                    fill=(110, 70, 40),
                    outline=HALO,
                )
            elif kind == "spring":
                draw.ellipse(
                    [cx - r / 1.5, cy - r / 1.5, cx + r / 1.5, cy + r / 1.5],
                    fill=STREAM,
                    outline=HALO,
                )
            elif kind == "camp":
                draw.polygon(
                    [(cx, cy - r), (cx - r, cy + r), (cx + r, cy + r)],
                    fill=(60, 130, 60),
                    outline=HALO,
                )
            elif kind == "parking":
                draw.rectangle(
                    [cx - r / 1.5, cy - r / 1.5, cx + r / 1.5, cy + r / 1.5],
                    fill=(70, 110, 190),
                    outline=HALO,
                )
            else:
                draw.ellipse(
                    [cx - r / 1.8, cy - r / 1.8, cx + r / 1.8, cy + r / 1.8],
                    fill=(200, 80, 60),
                    outline=HALO,
                )
        self._draw_labels(canvas, self._labels("map", zoom), bounds, cres)
        return self._finish(canvas, opaque=True)

    def contour_tile(self, unit: str, zoom: int, x: int, y: int) -> bytes | None:
        from PIL import Image, ImageDraw

        tree = self._contour_trees.get(unit)
        if tree is None:
            return None
        bounds, cres, size = self._frame(zoom, x, y)
        hits = [self.contours[unit][i] for i in self._query(tree, bounds)]
        if not hits:
            return None
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        scale = 2 ** ((zoom - 16) / 2.5)
        minor_w = max(1, round(1.0 * max(scale, 0.8) * SUPERSAMPLE))
        index_w = max(1, round(1.8 * max(scale, 0.8) * SUPERSAMPLE))
        for pass_index in (False, True):
            for line, _, is_index in hits:
                if is_index != pass_index or (not is_index and zoom < MINOR_CONTOURS_FROM_ZOOM):
                    continue
                for part in getattr(line, "geoms", [line]):
                    pts = self._to_px(part.coords, bounds, cres)
                    if len(pts) > 1:
                        draw.line(
                            pts,
                            fill=(CONTOUR_INDEX if is_index else CONTOUR_MINOR)
                            + (230 if is_index else 170,),
                            width=index_w if is_index else minor_w,
                            joint="curve",
                        )
        self._draw_labels(canvas, self._labels(f"contours_{unit}", zoom), bounds, cres)
        return self._finish(canvas, opaque=False)
