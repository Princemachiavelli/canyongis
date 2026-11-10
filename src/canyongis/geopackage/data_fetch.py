"""Download the source data for canyon tile layers: NAIP imagery and 1 m DEM tiles.

* NAIP: scenes are found through the Microsoft Planetary Computer STAC catalog and read
  directly as cloud-optimized GeoTIFFs from its Azure storage. Free, no account: each href is
  signed with a short-lived anonymous SAS token. (The AWS ``naip-analytic`` bucket is
  requester-pays and rejects anonymous reads.) The latest scene per quad is merged into one
  GeoTIFF clipped to the bbox.
* DEM: USGS 1 m DEM GeoTIFFs from The National Map, found with the canyongis TNM helpers.
  Tiles are downloaded to a cache, then merged and clipped to the bbox.

Both steps need rasterio, so they run inside the Nix environment. The pure helpers
(``latest_per_quad``, ``total_bytes``) are tested without it.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..io import log

NAIP_STAC_SEARCH = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
NAIP_SIGN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/sign"
NAIP_COLLECTION = "naip"
# Don't list the blob container for sidecar files on every open.
NAIP_ENV = {"GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR"}
_STAC_MAX_PAGES = 10
_USER_AGENT = "RopeGeo/1.0"


def quad_key(item_id: str) -> str:
    """NAIP quad identifier without resolution or dates (state, quad, quadrant, UTM zone).

    ``ca_m_3411752_nw_11_060_20180822_20190208`` -> ``ca_m_3411752_nw_11``. The resolution
    token differs between years (``.6``, ``060``, ``1``), so it must not be part of the key.
    """
    return "_".join(item_id.split("_")[:5])


def sign_href(href: str) -> str:
    """Planetary Computer SAS-signed URL for ``href`` (anonymous; token lasts about a day)."""
    req = urllib.request.Request(
        f"{NAIP_SIGN_URL}?{urllib.parse.urlencode({'href': href})}",
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["href"]


def latest_per_quad(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the most recent acquisition for each quad."""
    newest: dict[str, dict[str, Any]] = {}
    for item in items:
        key = quad_key(item["id"])
        if key not in newest or item["datetime"] > newest[key]["datetime"]:
            newest[key] = item
    return sorted(newest.values(), key=lambda i: i["id"])


def total_bytes(products: list[dict[str, Any]]) -> int:
    return sum(int(p.get("sizeInBytes") or 0) for p in products)


def naip_scenes(bbox_lonlat: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    """NAIP items intersecting the bbox, as ``{id, datetime, href, epsg}`` dicts."""
    body = {
        "collections": [NAIP_COLLECTION],
        "bbox": list(bbox_lonlat),
        "limit": 100,
    }
    items: list[dict[str, Any]] = []
    url: str | None = NAIP_STAC_SEARCH
    for _ in range(_STAC_MAX_PAGES):
        if url is None:
            break
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"User-Agent": _USER_AGENT, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            page = json.load(resp)
        for feature in page.get("features", []):
            asset = feature["assets"].get("image") or next(iter(feature["assets"].values()))
            items.append(
                {
                    "id": feature["id"],
                    "datetime": feature["properties"]["datetime"],
                    "href": asset["href"],
                    "epsg": feature["properties"].get("proj:epsg"),
                }
            )
        nxt = next((link for link in page.get("links", []) if link.get("rel") == "next"), None)
        url = nxt["href"] if nxt else None
        body = nxt.get("body", body) if nxt else body  # STAC pages POST a token in the body
    return latest_per_quad(items)


def _dominant_epsg(items: list[dict[str, Any]]) -> int | None:
    counts: dict[int, int] = {}
    for item in items:
        if item.get("epsg"):
            counts[int(item["epsg"])] = counts.get(int(item["epsg"]), 0) + 1
    return max(counts, key=counts.get) if counts else None


def build_naip_mosaic(bbox_lonlat: tuple[float, float, float, float], out_path: Path) -> Path:
    """Merge the latest NAIP scenes covering the bbox into ``out_path`` (GeoTIFF)."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import transform_bounds

    scenes = naip_scenes(bbox_lonlat)
    if not scenes:
        raise RuntimeError("no NAIP scenes found for this bbox")
    epsg = _dominant_epsg(scenes)
    log(f"NAIP: {len(scenes)} scenes, using EPSG:{epsg}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.Env(**NAIP_ENV):
        datasets = []
        for scene in scenes:
            if epsg is not None and scene.get("epsg") not in (None, epsg):
                continue
            log(f"  reading {scene['id']}")
            datasets.append(rasterio.open(sign_href(scene["href"])))
        try:
            crs = datasets[0].crs
            bounds = transform_bounds("EPSG:4326", crs, *bbox_lonlat)
            mosaic, transform = merge(datasets, bounds=bounds)
            profile = datasets[0].profile.copy()
            profile.update(
                driver="GTiff",
                height=mosaic.shape[1],
                width=mosaic.shape[2],
                transform=transform,
                count=mosaic.shape[0],
                compress="deflate",
                tiled=True,
                blockxsize=512,
                blockysize=512,
                BIGTIFF="IF_SAFER",
            )
            with rasterio.open(out_path, "w", **profile) as dst:
                dst.write(mosaic)
        finally:
            for ds in datasets:
                ds.close()
    return out_path


def dem_products(bbox_lonlat: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    from ..ingest import query_tnm_products

    return [p for p in query_tnm_products(bbox_lonlat) if p.get("downloadURL")]


def download_dem_tiles(products: list[dict[str, Any]], cache_dir: Path) -> list[Path]:
    from ..ingest import _download_file

    out_dir = cache_dir / "dem"
    paths = []
    for product in products:
        url = product["downloadURL"]
        path = out_dir / url.rsplit("/", 1)[-1]
        if not path.exists():
            log(f"  downloading {path.name} ({int(product.get('sizeInBytes') or 0) / 1e6:.0f} MB)")
            _download_file(url, path)
        paths.append(path)
    return paths


def build_dem_mosaic(
    paths: list[Path], bbox_lonlat: tuple[float, float, float, float], out_path: Path
) -> Path:
    """Merge 1 m DEM tiles into one float32 GeoTIFF clipped to the bbox."""
    import rasterio
    from rasterio.merge import merge
    from rasterio.warp import transform_bounds

    datasets = [rasterio.open(p) for p in paths]
    try:
        crs = datasets[0].crs
        same_crs = [ds for ds in datasets if ds.crs == crs]
        if len(same_crs) != len(datasets):
            log(f"DEM: skipping {len(datasets) - len(same_crs)} tiles in a different CRS")
        bounds = transform_bounds("EPSG:4326", crs, *bbox_lonlat)
        mosaic, transform = merge(same_crs, bounds=bounds, nodata=datasets[0].nodata)
        profile = same_crs[0].profile.copy()
        profile.update(
            driver="GTiff",
            height=mosaic.shape[1],
            width=mosaic.shape[2],
            transform=transform,
            count=1,
            dtype=mosaic.dtype,
            compress="deflate",
            tiled=True,
            BIGTIFF="IF_SAFER",
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(mosaic[:1])
    finally:
        for ds in datasets:
            ds.close()
    return out_path
