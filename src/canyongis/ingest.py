from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
from shapely.geometry import LineString, MultiLineString, box

from .io import log
from .validate import parse_kml

ROPEWIKI_API_URL = "https://ropewiki.com/api.php"
ROPEWIKI_PAGE_URL = "https://ropewiki.com/{title}"
TNM_API_URL = "https://tnmaccess.nationalmap.gov/api/v1/products"
TNM_1M_DATASET = "Digital Elevation Model (DEM) 1 meter"


@dataclass
class IngestResult:
    source: str
    page_title: str
    status: str
    kml_path: str | None = None
    dem_path: str | None = None
    page_url: str | None = None
    kml_url: str | None = None
    sciencebase_id: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class RopewikiPageRef:
    page_id: int | None = None
    title: str | None = None


def _http_get_json(url: str, params: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(full_url, headers={"User-Agent": "canyongis/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _download_file(url: str, path: Path, timeout: float = 300.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "canyongis/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, path.open("wb") as dst:
        shutil.copyfileobj(resp, dst)


def parse_ropewiki_ref(value: str) -> RopewikiPageRef:
    parsed = urllib.parse.urlparse(value.strip())
    if parsed.scheme and parsed.netloc:
        if parsed.netloc != "ropewiki.com":
            raise ValueError(f"unsupported Ropewiki host: {parsed.netloc}")
        path = parsed.path.strip("/")
        if path == "index.php":
            query = urllib.parse.parse_qs(parsed.query)
            curid = query.get("curid", [""])[0]
            if curid:
                return RopewikiPageRef(page_id=int(curid))
            title = query.get("title", [""])[0]
        else:
            title = path
        if not title:
            raise ValueError(f"could not derive Ropewiki title from {value!r}")
        return RopewikiPageRef(title=urllib.parse.unquote(title).replace(" ", "_"))
    text = value.strip()
    if text.isdigit():
        return RopewikiPageRef(page_id=int(text))
    return RopewikiPageRef(title=text.replace(" ", "_"))


def normalize_ropewiki_title(value: str) -> str:
    ref = parse_ropewiki_ref(value)
    if ref.title is None:
        raise ValueError(f"source does not contain a Ropewiki title: {value!r}")
    return ref.title


def _ropewiki_query(**params: Any) -> dict[str, Any]:
    base = {"format": "json", "formatversion": "2"}
    base.update(params)
    return _http_get_json(ROPEWIKI_API_URL, base)


def _get_page(ref: RopewikiPageRef) -> dict[str, Any]:
    query: dict[str, Any] = {"action": "query", "prop": "images"}
    if ref.page_id is not None:
        query["pageids"] = str(ref.page_id)
    elif ref.title is not None:
        query["titles"] = ref.title
    else:
        raise ValueError("Ropewiki page reference is empty")
    data = _ropewiki_query(**query)
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        raise RuntimeError(f"Ropewiki page not found: {ref}")
    page = pages[0]
    if page.get("missing"):
        raise RuntimeError(f"Ropewiki page not found: {ref}")
    return page


def _find_kml_file_title(ref: RopewikiPageRef) -> tuple[str | None, str]:
    if ref.title and ref.title.lower().startswith("file:") and ref.title.lower().endswith(".kml"):
        return ref.title, ref.title.removeprefix("File:")
    page = _get_page(ref)
    images = page.get("images") or []
    kml_titles = sorted(
        image["title"]
        for image in images
        if isinstance(image, dict) and str(image.get("title", "")).lower().endswith(".kml")
    )
    page_title = str(page.get("title") or ref.title or ref.page_id or "unknown")
    return (kml_titles[0] if kml_titles else None), page_title


def _get_kml_download_url(file_title: str) -> str | None:
    data = _ropewiki_query(
        action="query",
        titles=file_title,
        prop="imageinfo",
        iiprop="url",
    )
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        raise RuntimeError(f"Ropewiki file not found: {file_title}")
    info = (pages[0].get("imageinfo") or [{}])[0]
    url = info.get("url")
    return None if not url else str(url)


def _manifest_dem_paths(manifest_path: str | Path | None) -> list[Path]:
    if manifest_path is None:
        return []
    path = Path(manifest_path)
    if not path.exists():
        return []
    rows = json.loads(path.read_text())
    dems = []
    for row in rows:
        dem_path = row.get("dem_path")
        if dem_path:
            dem = Path(dem_path)
            if dem.exists():
                dems.append(dem)
    return dems


def _cache_dem_paths(cache_dirs: list[str | Path]) -> list[Path]:
    dems: list[Path] = []
    seen: set[Path] = set()
    for cache_dir in cache_dirs:
        root = Path(cache_dir)
        if not root.exists():
            continue
        for meta_path in root.glob("*/meta.json"):
            try:
                dem_path = Path(json.loads(meta_path.read_text()).get("dem_path", ""))
            except json.JSONDecodeError:
                continue
            if dem_path.exists() and dem_path not in seen:
                seen.add(dem_path)
                dems.append(dem_path)
    return dems


def list_local_dem_paths(
    manifest_path: str | Path | None,
    cache_dirs: list[str | Path],
    dem_dir: str | Path | None = None,
) -> list[Path]:
    seen: set[Path] = set()
    paths: list[Path] = []
    for dem_path in _manifest_dem_paths(manifest_path) + _cache_dem_paths(cache_dirs):
        if dem_path not in seen:
            seen.add(dem_path)
            paths.append(dem_path)
    if dem_dir is not None:
        for pattern in ("*.tif", "*.tiff"):
            for path in sorted(Path(dem_dir).glob(pattern)):
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
    return paths


def _dem_area(bounds: rasterio.coords.BoundingBox) -> float:
    return float(bounds.right - bounds.left) * float(bounds.top - bounds.bottom)


def dem_covers_bounds(
    dem_path: str | Path, bounds_wgs84: tuple[float, float, float, float]
) -> bool:
    with rasterio.open(dem_path) as src:
        dem_bounds = src.bounds
        candidate = transform_bounds("EPSG:4326", src.crs, *bounds_wgs84, densify_pts=21)
    return (
        dem_bounds.left <= candidate[0]
        and dem_bounds.bottom <= candidate[1]
        and dem_bounds.right >= candidate[2]
        and dem_bounds.top >= candidate[3]
    )


def dem_descent_coverage_counts(kml_path: str | Path, dem_path: str | Path) -> tuple[int, int]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError(f"KML has no descent lines: {kml_path}")

    with rasterio.open(dem_path) as src:
        descent = features.descent_lines.to_crs(src.crs)
        dem_bounds = box(*src.bounds)
        descent_bounds = box(*descent.total_bounds)
        if not dem_bounds.covers(descent_bounds):
            raise ValueError(f"DEM bounds do not fully cover descent bounds: {kml_path} -> {dem_path}")

        geoms = [
            (geom, 1)
            for geom in descent.geometry
            if isinstance(geom, (LineString, MultiLineString)) and not geom.is_empty
        ]
        if not geoms:
            raise ValueError(f"KML has no rasterizable descent geometries: {kml_path}")

        line_mask = rasterize(
            geoms,
            out_shape=(src.height, src.width),
            transform=src.transform,
            fill=0,
            dtype=np.uint8,
            all_touched=True,
        )
        line_pixels = int(line_mask.sum())
        if line_pixels == 0:
            raise ValueError(f"descent line rasterizes to zero pixels: {kml_path} -> {dem_path}")

        arr = src.read(1, masked=True)
        mask = np.ma.getmaskarray(arr)
        values = np.asarray(arr.filled(np.nan), dtype=np.float32)
        valid_dem = (~mask) & np.isfinite(values)
        valid_line_pixels = int((valid_dem & (line_mask == 1)).sum())

    return valid_line_pixels, line_pixels


def dem_has_valid_descent_coverage(kml_path: str | Path, dem_path: str | Path) -> bool:
    valid_line_pixels, line_pixels = dem_descent_coverage_counts(kml_path, dem_path)
    return valid_line_pixels == line_pixels


def find_covering_dem(
    dem_paths: list[str | Path],
    bounds_wgs84: tuple[float, float, float, float],
    kml_path: str | Path | None = None,
) -> Path | None:
    best: tuple[float, Path] | None = None
    for dem_path in dem_paths:
        try:
            if not dem_covers_bounds(dem_path, bounds_wgs84):
                continue
            if kml_path is not None:
                valid, total = dem_descent_coverage_counts(kml_path, dem_path)
                if valid != total:
                    log(
                        f"skipping DEM with nodata under descent track: {dem_path} "
                        f"({valid}/{total} valid descent pixels)"
                    )
                    continue
            with rasterio.open(dem_path) as src:
                area = _dem_area(src.bounds)
        except Exception:
            continue
        path = Path(dem_path)
        if best is None or area < best[0]:
            best = (area, path)
    return None if best is None else best[1]


def _extract_bounds(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    for key in ("boundingBox", "bestFitIndex", "extent"):
        value = item.get(key)
        if isinstance(value, dict):
            min_x = value.get("minX", value.get("xmin"))
            min_y = value.get("minY", value.get("ymin"))
            max_x = value.get("maxX", value.get("xmax"))
            max_y = value.get("maxY", value.get("ymax"))
            if None not in (min_x, min_y, max_x, max_y):
                return (float(min_x), float(min_y), float(max_x), float(max_y))
    return None


def _item_download_url(item: dict[str, Any]) -> str | None:
    for key in ("downloadURL", "downloadUrl"):
        value = item.get(key)
        if value:
            return str(value)
    urls = item.get("urls")
    if isinstance(urls, dict):
        for key in ("download", "TIFF", "GeoTIFF"):
            value = urls.get(key)
            if value:
                return str(value)
    return None


def query_tnm_products(bounds_wgs84: tuple[float, float, float, float]) -> list[dict[str, Any]]:
    data = _http_get_json(
        TNM_API_URL,
        {
            "datasets": TNM_1M_DATASET,
            "bbox": ",".join(f"{coord:.8f}" for coord in bounds_wgs84),
            "prodFormats": "GeoTIFF",
            "max": 50,
        },
    )
    return list(data.get("items", []))


def choose_tnm_product(
    items: list[dict[str, Any]], bounds_wgs84: tuple[float, float, float, float]
) -> dict[str, Any]:
    if not items:
        raise RuntimeError("TNM returned no 1-meter DEM products for the KML extent")
    candidates = _rank_tnm_products(items, bounds_wgs84)
    if not candidates:
        raise RuntimeError("TNM returned no 1-meter DEM products with download URLs")
    return candidates[0]


def _rank_tnm_products(
    items: list[dict[str, Any]], bounds_wgs84: tuple[float, float, float, float]
) -> list[dict[str, Any]]:
    containing = []
    for item in items:
        item_bounds = _extract_bounds(item)
        if item_bounds is None:
            continue
        if (
            item_bounds[0] <= bounds_wgs84[0]
            and item_bounds[1] <= bounds_wgs84[1]
            and item_bounds[2] >= bounds_wgs84[2]
            and item_bounds[3] >= bounds_wgs84[3]
            ):
            containing.append(item)
    candidates = containing or items
    candidates.sort(key=lambda item: str(item.get("title", "")))
    candidates.sort(key=lambda item: str(item.get("publicationDate", "")), reverse=True)
    candidates.sort(key=lambda item: _item_download_url(item) is None)
    return [item for item in candidates if _item_download_url(item) is not None]


def _bounds_from_kml(kml_path: str | Path) -> tuple[float, float, float, float]:
    features = parse_kml(kml_path)
    if features.descent_lines.empty:
        raise ValueError("KML does not contain any descent track")
    return tuple(float(v) for v in features.descent_lines.total_bounds)


def _safe_filename(url: str, fallback: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).name
    return name or fallback


def append_manifest_entries(
    manifest_path: str | Path,
    entries: list[dict[str, str]],
) -> None:
    path = Path(manifest_path)
    existing = json.loads(path.read_text()) if path.exists() else []
    existing_pairs = {(row.get("kml_path"), row.get("dem_path")) for row in existing}
    for entry in entries:
        pair = (entry["kml_path"], entry["dem_path"])
        if pair not in existing_pairs:
            existing.append(entry)
            existing_pairs.add(pair)
    path.write_text(json.dumps(existing, indent=2) + "\n")


def ingest_ropewiki_page(
    source: str,
    *,
    kml_dir: str | Path,
    dem_dir: str | Path,
    manifest_path: str | Path | None,
    cache_dirs: list[str | Path],
    dry_run: bool = False,
) -> IngestResult:
    ref = parse_ropewiki_ref(source)
    page_title = ref.title or str(ref.page_id)
    page_url = (
        ROPEWIKI_PAGE_URL.format(title=page_title)
        if ref.title is not None
        else f"https://ropewiki.com/index.php?curid={ref.page_id}"
    )

    kml_file_title, resolved_page_title = _find_kml_file_title(ref)
    page_title = resolved_page_title
    if ref.title is None:
        page_url = ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_"))
    if kml_file_title is None:
        return IngestResult(
            source=source,
            page_title=page_title,
            page_url=page_url,
            status="skipped_no_kml",
            message="Ropewiki page does not have an attached KML file",
        )

    kml_url = _get_kml_download_url(kml_file_title)
    if kml_url is None:
        return IngestResult(
            source=source,
            page_title=page_title,
            page_url=page_url,
            status="skipped_no_kml",
            message=f"Ropewiki file lacks a download URL: {kml_file_title}",
        )
    kml_name = _safe_filename(kml_url, f"{page_title}.kml")
    kml_path = Path(kml_dir) / kml_name
    inspect_kml_path = kml_path
    downloaded_kml = False
    if not kml_path.exists():
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="canyongis-ropewiki-") as tmpdir:
                inspect_kml_path = Path(tmpdir) / kml_name
                log(f"downloading KML for inspection: {kml_url}")
                _download_file(kml_url, inspect_kml_path)
                try:
                    bounds_wgs84 = _bounds_from_kml(inspect_kml_path)
                except ValueError as exc:
                    return IngestResult(
                        source=source,
                        page_title=page_title,
                        page_url=page_url,
                        kml_url=kml_url,
                        status="skipped_no_descent",
                        message=str(exc),
                    )
        else:
            log(f"downloading KML: {kml_url}")
            kml_path.parent.mkdir(parents=True, exist_ok=True)
            _download_file(kml_url, kml_path)
            downloaded_kml = True

    if kml_path.exists():
        try:
            bounds_wgs84 = _bounds_from_kml(inspect_kml_path)
        except ValueError as exc:
            if downloaded_kml and kml_path.exists():
                kml_path.unlink()
            return IngestResult(
                source=source,
                page_title=page_title,
                page_url=page_url,
                kml_url=kml_url,
                status="skipped_no_descent",
                message=str(exc),
            )

    dem_candidates = list_local_dem_paths(manifest_path, cache_dirs, dem_dir)
    existing_dem = find_covering_dem(dem_candidates, bounds_wgs84, inspect_kml_path)
    if existing_dem is not None:
        return IngestResult(
            source=source,
            page_title=page_title,
            page_url=page_url,
            kml_url=kml_url,
            kml_path=str(kml_path),
            dem_path=str(existing_dem),
            status="reused_dem",
        )

    items = query_tnm_products(bounds_wgs84)
    candidates = _rank_tnm_products(items, bounds_wgs84)
    if not candidates:
        return IngestResult(
            source=source,
            page_title=page_title,
            page_url=page_url,
            kml_url=kml_url,
            kml_path=str(kml_path),
            status="skipped_no_dem",
            message="TNM returned no 1-meter DEM products with download URLs",
        )
    coverage_errors: list[str] = []
    for chosen in candidates:
        dem_url = _item_download_url(chosen)
        assert dem_url is not None
        dem_name = _safe_filename(dem_url, f"{page_title}.tif")
        dem_path = Path(dem_dir) / dem_name
        if not dry_run and not dem_path.exists():
            log(f"downloading DEM: {dem_url}")
            dem_path.parent.mkdir(parents=True, exist_ok=True)
            _download_file(dem_url, dem_path)
        if dry_run:
            return IngestResult(
                source=source,
                page_title=page_title,
                page_url=page_url,
                kml_url=kml_url,
                kml_path=str(kml_path),
                dem_path=str(dem_path),
                sciencebase_id=chosen.get("sourceId") or chosen.get("sourceid"),
                status="planned_download",
            )
        try:
            if dem_has_valid_descent_coverage(inspect_kml_path, dem_path):
                return IngestResult(
                    source=source,
                    page_title=page_title,
                    page_url=page_url,
                    kml_url=kml_url,
                    kml_path=str(kml_path),
                    dem_path=str(dem_path),
                    sciencebase_id=chosen.get("sourceId") or chosen.get("sourceid"),
                    status="downloaded_dem",
                )
            valid, total = dem_descent_coverage_counts(inspect_kml_path, dem_path)
            coverage_errors.append(f"{dem_path} ({valid}/{total} valid descent pixels)")
        except Exception as exc:
            coverage_errors.append(f"{dem_path} ({exc})")
    detail = "; ".join(coverage_errors)
    return IngestResult(
        source=source,
        page_title=page_title,
        page_url=page_url,
        kml_url=kml_url,
        kml_path=str(kml_path),
        status="skipped_no_dem",
        message=f"TNM DEM products lacked complete valid descent coverage: {detail}",
    )


def ingest_ropewiki_cli(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(
        prog="canyongis-ropewiki-sync",
        description="Download Ropewiki KMLs and matching USGS 1-meter DEMs for training.",
    )
    p.add_argument("pages", nargs="+", help="Ropewiki page URLs or page titles.")
    p.add_argument(
        "--manifest",
        type=Path,
        default=Path("training-manifest.json"),
        help="Manifest to use for existing DEM discovery and to append new entries to.",
    )
    p.add_argument(
        "--kml-dir",
        type=Path,
        default=Path("data/ropewiki/kml"),
        help="Directory for downloaded KML files.",
    )
    p.add_argument(
        "--dem-dir",
        type=Path,
        default=Path("data/ropewiki/dem"),
        help="Directory for downloaded DEM GeoTIFFs.",
    )
    p.add_argument(
        "--cache-dir",
        action="append",
        default=[],
        help="Training cache root(s) whose meta.json files should be scanned for reusable DEMs.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve assets and print decisions without writing files or editing the manifest.",
    )
    args = p.parse_args(argv)

    added_entries: list[dict[str, str]] = []
    for source in args.pages:
        try:
            result = ingest_ropewiki_page(
                source,
                kml_dir=args.kml_dir,
                dem_dir=args.dem_dir,
                manifest_path=args.manifest,
                cache_dirs=args.cache_dir,
                dry_run=args.dry_run,
            )
        except Exception as exc:
            log(f"ERROR {source}: {exc}")
            continue

        msg = f"{result.status}: {result.page_title}"
        if result.message:
            msg = f"{msg} ({result.message})"
        log(msg)
        if result.kml_path and result.dem_path:
            added_entries.append({"kml_path": result.kml_path, "dem_path": result.dem_path})

    if added_entries and not args.dry_run:
        append_manifest_entries(args.manifest, added_entries)
        log(f"appended {len(added_entries)} entries to {args.manifest}")
