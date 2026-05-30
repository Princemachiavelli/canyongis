from __future__ import annotations

import html
import json
import re
import urllib.parse
import urllib.request
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

import geopandas as gpd
from pydantic import BaseModel, Field

from ..aoi_mosaic import build_aoi_dem_mosaic
from ..ingest import (
    ROPEWIKI_PAGE_URL,
    _download_file,
    _find_kml_file_title,
    _get_kml_download_url,
    get_ropewiki_page_coords,
    list_ropewiki_canyons_in_bbox,
    parse_ropewiki_ref,
)
from ..validate import parse_kml
from .auth import User
from .dem_jobs import DemJobSpec, InputSpec, LayerSpec, OutputSpec, register


class Bounds(BaseModel):
    west: float
    south: float
    east: float
    north: float

    def tuple(self) -> tuple[float, float, float, float]:  # type: ignore[override]
        if self.west >= self.east:
            raise ValueError("west must be < east")
        if self.south >= self.north:
            raise ValueError("south must be < north")
        return (self.west, self.south, self.east, self.north)


class BuildDemToolRequest(BaseModel):
    name: str = Field(default="aoi", min_length=1)
    bounds: Bounds
    buffer_m: float = 200.0
    max_products: int = 24


class RopewikiKmlImportRequest(BaseModel):
    source: str = Field(..., min_length=1)
    name: str | None = None
    container_dem_job_id: str | None = None


class BulkRopewikiKmlImportRequest(BaseModel):
    bounds: Bounds
    container_dem_job_id: str | None = None
    max_results: int = Field(default=50, ge=1, le=500)


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value).strip("_")
    return safe or "aoi"


def _safe_kml_name(value: str) -> str:
    name = _safe_name(Path(value).name)
    if not name.lower().endswith(".kml"):
        name = f"{Path(name).stem}.kml"
    return name


def _gdf_to_geojson(gdf: gpd.GeoDataFrame) -> dict[str, Any]:
    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}
    return json.loads(gdf.to_json())


def _bounds_contains(
    outer: tuple[float, float, float, float] | list[float],
    inner: tuple[float, float, float, float] | list[float],
    *,
    epsilon: float = 1e-9,
) -> bool:
    ow, os, oe, on = (float(v) for v in outer)
    iw, is_, ie, in_ = (float(v) for v in inner)
    return iw >= ow - epsilon and is_ >= os - epsilon and ie <= oe + epsilon and in_ <= on + epsilon


def _validate_kml_container(
    ctx: Any, container_dem_job_id: str | None, bounds: list[float]
) -> None:
    if not container_dem_job_id:
        return
    dem_job = ctx.job_or_404(container_dem_job_id)
    if dem_job.status != "succeeded" or not dem_job.result:
        raise ValueError("target DEM container has not succeeded")
    dem_bounds = dem_job.result.get("bounds_wgs84")
    if not isinstance(dem_bounds, list) or len(dem_bounds) != 4:
        raise ValueError("target DEM container is missing bounds")
    if not _bounds_contains(dem_bounds, bounds):
        raise ValueError(
            "KML bounds are outside the selected DEM container; choose a different container "
            "or build a DEM for this KML first"
        )


def _set_kml_container_params(params: dict[str, Any], container_dem_job_id: str | None) -> None:
    params["container_dem_job_id"] = container_dem_job_id
    inputs = params.get("_inputs")
    inputs = dict(inputs) if isinstance(inputs, dict) else {}
    if container_dem_job_id:
        inputs["container"] = container_dem_job_id
    else:
        inputs.pop("container", None)
    if inputs:
        params["_inputs"] = inputs
    else:
        params.pop("_inputs", None)


def _assign_kml_container_if_valid(
    ctx: Any,
    result: dict[str, Any],
    params: dict[str, Any],
    container_dem_job_id: str | None,
    progress_cb,
) -> None:
    if not container_dem_job_id:
        _set_kml_container_params(params, None)
        result.pop("container_dem_job_id", None)
        return
    try:
        _validate_kml_container(ctx, container_dem_job_id, result["bounds_wgs84"])
    except Exception as exc:
        progress_cb(f"KML not assigned to DEM container {container_dem_job_id}: {exc}")
        _set_kml_container_params(params, None)
        result.pop("container_dem_job_id", None)
        return
    _set_kml_container_params(params, container_dem_job_id)
    result["container_dem_job_id"] = container_dem_job_id


def _kml_import_path(ctx: Any, job_id: str, name: str) -> Path:
    return ctx.output_root / "kml" / job_id / _safe_kml_name(name)


KmlSourceKind = Literal["ropewiki", "caltopo", "url"]


def classify_kml_source(source: str) -> KmlSourceKind:
    """Classify a kml-import ``source`` string into a fetch strategy."""
    text = source.strip()
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme and parsed.netloc:
        host = parsed.netloc.lower().removeprefix("www.")
        if host == "ropewiki.com":
            return "ropewiki"
        if host == "caltopo.com" or host.endswith(".caltopo.com"):
            return "caltopo"
        return "url"
    return "ropewiki"


def _caltopo_map_id(url: str) -> str | None:
    """Extract the map ID from a ``caltopo.com/m/<ID>`` URL, ignoring # / .ext / slug."""
    parsed = urllib.parse.urlparse(url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "m":
        return parts[1].split(".")[0].split("#")[0] or None
    return None


def caltopo_kml_url(url: str) -> str:
    """Rewrite a public CalTopo map URL to the JSON API endpoint that lists features.

    CalTopo's classic ``.kml`` endpoint is an authenticated NetworkLink wrapper that
    redirects to a login page for anonymous fetchers, so we use the public
    ``/api/v1/map/<ID>/since/0`` JSON feed instead and convert features to KML
    in ``_caltopo_features_to_kml``.
    """
    map_id = _caltopo_map_id(url)
    if map_id is None:
        return urllib.parse.urlunparse(urllib.parse.urlparse(url.strip()))
    return f"https://caltopo.com/api/v1/map/{map_id}/since/0"


def _caltopo_coord(c: Any) -> tuple[float, float] | None:
    """Normalize one CalTopo coordinate triple/quad into ``(lon, lat)``."""
    if not isinstance(c, list) or len(c) < 2:
        return None
    try:
        return (float(c[0]), float(c[1]))
    except (TypeError, ValueError):
        return None


def _caltopo_coord_string(coords: Any) -> str:
    """Join a ring/line of CalTopo coords into a KML ``coordinates`` value."""
    parts: list[str] = []
    if isinstance(coords, list):
        for raw in coords:
            normalized = _caltopo_coord(raw)
            if normalized is None:
                continue
            lon, lat = normalized
            parts.append(f"{lon},{lat},0")
    return " ".join(parts)


def _caltopo_features_to_kml(data: dict[str, Any], *, document_name: str) -> bytes:
    """Convert a CalTopo ``/since/0`` JSON payload into a minimal KML document."""
    features = (
        ((data.get("result") or {}).get("state") or {}).get("features") or []
        if isinstance(data, dict)
        else []
    )
    placemarks: list[str] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geom = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        props = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        title = html.escape(str(props.get("title") or props.get("name") or "Untitled")[:512])
        description = html.escape(str(props.get("description") or "")[:2048])
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Point":
            normalized = _caltopo_coord(coords)
            if normalized is None:
                continue
            lon, lat = normalized
            placemarks.append(
                f"<Placemark><name>{title}</name>"
                f"<description>{description}</description>"
                f"<Point><coordinates>{lon},{lat},0</coordinates></Point></Placemark>"
            )
        elif gtype == "LineString":
            coord_str = _caltopo_coord_string(coords)
            if not coord_str:
                continue
            placemarks.append(
                f"<Placemark><name>{title}</name>"
                f"<description>{description}</description>"
                f"<LineString><tessellate>1</tessellate>"
                f"<coordinates>{coord_str}</coordinates></LineString></Placemark>"
            )
        elif gtype == "Polygon" and isinstance(coords, list) and coords:
            coord_str = _caltopo_coord_string(coords[0])
            if not coord_str:
                continue
            placemarks.append(
                f"<Placemark><name>{title}</name>"
                f"<description>{description}</description>"
                f"<Polygon><outerBoundaryIs><LinearRing>"
                f"<coordinates>{coord_str}</coordinates>"
                f"</LinearRing></outerBoundaryIs></Polygon></Placemark>"
            )
    body = "\n".join(placemarks)
    document_label = html.escape(document_name)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n'
        f"<name>{document_label}</name>\n"
        + body
        + "\n</Document></kml>"
    ).encode("utf-8")


_NETWORK_LINK_HREF_RE = re.compile(
    r"<NetworkLink\b[^>]*>.*?<Link\b[^>]*>.*?<href>\s*([^<]+?)\s*</href>",
    re.IGNORECASE | re.DOTALL,
)


def _network_link_target(content: bytes, base_url: str) -> str | None:
    """If ``content`` is a NetworkLink wrapper KML, return the absolute href inside."""
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:
        return None
    if "<NetworkLink" not in text:
        return None
    match = _NETWORK_LINK_HREF_RE.search(text)
    if not match:
        return None
    href = html.unescape(match.group(1).strip())
    return urllib.parse.urljoin(base_url, href)


def _fetch_url_bytes(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "canyongis/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _fetch_kml_following_network_link(
    url: str, *, progress_cb, max_hops: int = 3
) -> tuple[bytes, str]:
    """Fetch ``url``; if it's a NetworkLink wrapper, follow the inner href.

    Returns ``(content_bytes, final_url)``. Bounded by ``max_hops`` to avoid loops.
    """
    current = url
    for _ in range(max_hops):
        content = _fetch_url_bytes(current)
        target = _network_link_target(content, current)
        if not target or target == current:
            return content, current
        progress_cb(f"follow NetworkLink -> {target}")
        current = target
    raise ValueError(f"NetworkLink chain exceeded {max_hops} hops starting at {url}")


def _kml_filename_from_url(url: str, default_stem: str) -> str:
    parsed = urllib.parse.urlparse(url)
    candidate = Path(urllib.parse.unquote(parsed.path)).name
    if not candidate or not candidate.lower().endswith(".kml"):
        candidate = f"{default_stem}.kml"
    return _safe_kml_name(candidate)


def _looks_like_kml(content: bytes) -> bool:
    snippet = content[:512].lstrip().lower()
    return snippet.startswith(b"<?xml") or snippet.startswith(b"<kml")


def _fetch_caltopo_kml_bytes(
    *, source: str, fetch_url: str, document_name: str, progress_cb
) -> bytes:
    """Fetch a CalTopo ``/since/0`` JSON and convert it to KML bytes."""
    progress_cb(f"download CalTopo JSON: {fetch_url}")
    raw = _fetch_url_bytes(fetch_url)
    if not raw:
        raise ValueError(f"CalTopo returned an empty response: {fetch_url}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"CalTopo did not return JSON: {fetch_url}") from exc
    kml_bytes = _caltopo_features_to_kml(data, document_name=document_name)
    if b"<Placemark" not in kml_bytes:
        raise ValueError(f"CalTopo map has no importable features: {source}")
    return kml_bytes


def _import_kml_from_url(
    ctx: Any,
    *,
    job_id: str,
    source: str,
    fetch_url: str,
    source_type: Literal["caltopo", "url"],
    default_name: str | None,
    progress_cb,
) -> tuple[Path, dict[str, Any], str]:
    """Fetch a remote KML, save under the job's output dir, return ``(path, source_dict, display_name)``."""
    content, resolved_url = _fetch_kml_following_network_link(
        fetch_url, progress_cb=progress_cb
    )
    if not content:
        raise ValueError(f"remote KML is empty: {fetch_url}")
    if not _looks_like_kml(content):
        raise ValueError(f"URL did not return KML content: {resolved_url}")
    fallback_stem = _safe_name(default_name or "imported").removesuffix(".kml") or "imported"
    filename = _kml_filename_from_url(resolved_url, fallback_stem)
    display_name = (default_name or Path(filename).stem).strip() or Path(filename).stem
    kml_path = _kml_import_path(ctx, job_id, filename)
    kml_path.parent.mkdir(parents=True, exist_ok=True)
    kml_path.write_bytes(content)
    source_dict: dict[str, Any] = {
        "type": source_type,
        "source": source,
        "download_url": fetch_url,
    }
    if resolved_url != fetch_url:
        source_dict["resolved_url"] = resolved_url
    return kml_path, source_dict, display_name


def build_kml_import_result(
    ctx: Any,
    kml_path: Path,
    name: str,
    *,
    source: dict[str, Any],
    container_dem_job_id: str | None = None,
) -> dict[str, Any]:
    features = parse_kml(kml_path)
    all_geoms = []
    for gdf in (
        features.descent_lines,
        features.approach_lines,
        features.unclassified_lines,
        features.points,
    ):
        if not gdf.empty:
            all_geoms.extend(gdf.geometry.tolist())
    if not all_geoms:
        raise ValueError("KML contains no geometry (no LineStrings or Points)")
    combined = gpd.GeoSeries(all_geoms, crs="EPSG:4326")
    west, south, east, north = (float(v) for v in combined.total_bounds)
    result = {
        "name": name,
        "kml_path": str(kml_path),
        "kml_url": ctx.output_url(kml_path),
        "out_dir": str(kml_path.parent),
        "bounds_wgs84": [west, south, east, north],
        "descent_geojson": _gdf_to_geojson(features.descent_lines),
        "approach_geojson": _gdf_to_geojson(features.approach_lines),
        "unclassified_geojson": _gdf_to_geojson(features.unclassified_lines),
        "points_geojson": _gdf_to_geojson(features.points),
        "counts": {
            "descent_lines": len(features.descent_lines),
            "approach_lines": len(features.approach_lines),
            "unclassified_lines": len(features.unclassified_lines),
            "points": len(features.points),
        },
        "source": source,
    }
    if container_dem_job_id:
        result["container_dem_job_id"] = container_dem_job_id
    return result


def _run_build_dem(*, ctx: Any, req: BuildDemToolRequest, progress_cb, **_: Any) -> dict[str, Any]:
    name = _safe_name(req.name)
    out_dir = ctx.output_root / "areas" / name
    dem_path = out_dir / "dem.tif"
    progress_cb(f"build-dem -> {dem_path} (buffer_m={req.buffer_m})")
    result = build_aoi_dem_mosaic(
        req.bounds.tuple(),
        dem_path,
        cache_dir=ctx.dem_cache,
        buffer_m=req.buffer_m,
        max_products=req.max_products,
    )
    return {
        **asdict(result),
        "name": name,
        "dem_url": ctx.output_url(result.dem_path),
        "bounds_wgs84": result.bounds_wgs84,
    }


def _ropewiki_waypoint_result(
    ctx: Any,
    *,
    ref: Any,
    source: str,
    page_title: str | None,
    page_url: str,
    kml_path: Path,
    display_name: str,
    progress_cb,
    skip_reason: str,
) -> dict[str, Any]:
    """Build a Ropewiki ``build_kml_import_result`` from page GPS coords.

    Raises ``ValueError`` if the page has no coordinates either. ``skip_reason``
    is appended to progress so users can see why we fell back to a waypoint.
    """
    coords = get_ropewiki_page_coords(ref)
    if coords is None:
        raise ValueError(
            f"Ropewiki page has no usable KML and no GPS coordinates ({skip_reason}): {source}"
        )
    lat, lon = coords
    kml_path.parent.mkdir(parents=True, exist_ok=True)
    progress_cb(
        f"Ropewiki page has no KML ({skip_reason}); generating waypoint {display_name} "
        f"at {lat:.5f},{lon:.5f}"
    )
    _write_ropewiki_waypoint_kml(
        kml_path, name=display_name, lat=lat, lon=lon, page_url=page_url
    )
    return build_kml_import_result(
        ctx,
        kml_path,
        display_name,
        source={
            "type": "ropewiki",
            "source": source,
            "page_title": page_title,
            "page_url": page_url,
            "file_title": None,
            "download_url": None,
            "waypoint_only": True,
            "latitude": lat,
            "longitude": lon,
        },
    )


def _write_ropewiki_waypoint_kml(
    path: Path, *, name: str, lat: float, lon: float, page_url: str
) -> None:
    """Write a minimal KML containing one Placemark/Point with name and link."""
    description = (
        f'<![CDATA[<a href="{html.escape(page_url)}">{html.escape(page_url)}</a>]]>'
    )
    safe_name = html.escape(name)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
        '<Style id="rwWaypoint"><IconStyle><color>ff00ffff</color>'
        "<scale>1.0</scale></IconStyle></Style>",
        f"<Placemark><name>{safe_name}</name>"
        f"<description>{description}</description>"
        '<styleUrl>#rwWaypoint</styleUrl>'
        f"<Point><coordinates>{lon},{lat},0</coordinates></Point>"
        "</Placemark>",
        "</Document></kml>",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _run_ropewiki_kml_import(
    *,
    ctx: Any,
    req: RopewikiKmlImportRequest,
    params: dict[str, Any],
    progress_cb,
    job_id: str,
    **_: Any,
) -> dict[str, Any]:
    source = req.source.strip()
    if not source:
        raise ValueError("source is required")
    source_kind = classify_kml_source(source)

    if source_kind == "caltopo":
        map_id = _caltopo_map_id(source) or "caltopo-map"
        fetch_url = caltopo_kml_url(source)
        display_name = (req.name or map_id).strip() or map_id
        filename = _safe_kml_name(f"{display_name}.kml")
        kml_path = _kml_import_path(ctx, job_id, filename)
        kml_path.parent.mkdir(parents=True, exist_ok=True)
        kml_bytes = _fetch_caltopo_kml_bytes(
            source=source,
            fetch_url=fetch_url,
            document_name=display_name,
            progress_cb=progress_cb,
        )
        kml_path.write_bytes(kml_bytes)
        source_dict = {
            "type": "caltopo",
            "source": source,
            "download_url": fetch_url,
            "map_id": map_id,
        }
        result = build_kml_import_result(ctx, kml_path, display_name, source=source_dict)
        _assign_kml_container_if_valid(ctx, result, params, req.container_dem_job_id, progress_cb)
        return result

    if source_kind == "url":
        progress_cb(f"download KML from URL: {source}")
        kml_path, source_dict, display_name = _import_kml_from_url(
            ctx,
            job_id=job_id,
            source=source,
            fetch_url=source,
            source_type="url",
            default_name=req.name,
            progress_cb=progress_cb,
        )
        result = build_kml_import_result(ctx, kml_path, display_name, source=source_dict)
        _assign_kml_container_if_valid(ctx, result, params, req.container_dem_job_id, progress_cb)
        return result

    ref = parse_ropewiki_ref(source)
    file_title, page_title = _find_kml_file_title(ref)
    page_url = (
        ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_")) if page_title else source
    )

    download_url = _get_kml_download_url(file_title) if file_title else None

    if file_title is None or download_url is None:
        skip_reason = (
            "no KML attachment"
            if file_title is None
            else f"attachment {file_title} has no download URL"
        )
        waypoint_name = (req.name or page_title or "Ropewiki waypoint").strip() or "Ropewiki"
        waypoint_path = _kml_import_path(ctx, job_id, _safe_kml_name(f"{waypoint_name}.kml"))
        result = _ropewiki_waypoint_result(
            ctx,
            ref=ref,
            source=source,
            page_title=page_title,
            page_url=page_url,
            kml_path=waypoint_path,
            display_name=waypoint_name,
            progress_cb=progress_cb,
            skip_reason=skip_reason,
        )
        _assign_kml_container_if_valid(ctx, result, params, req.container_dem_job_id, progress_cb)
        return result

    filename = _safe_kml_name(file_title.removeprefix("File:"))
    display_name = (req.name or page_title or Path(filename).stem).strip() or Path(filename).stem
    kml_path = _kml_import_path(ctx, job_id, filename)
    kml_path.parent.mkdir(parents=True, exist_ok=True)
    progress_cb(f"download Ropewiki KML {file_title} -> {kml_path}")
    _download_file(download_url, kml_path)
    result = build_kml_import_result(
        ctx,
        kml_path,
        display_name,
        source={
            "type": "ropewiki",
            "source": source,
            "page_title": page_title,
            "page_url": page_url,
            "file_title": file_title,
            "download_url": download_url,
        },
    )
    _assign_kml_container_if_valid(ctx, result, params, req.container_dem_job_id, progress_cb)
    return result


def _run_bulk_ropewiki_kml_import(
    *,
    ctx: Any,
    req: BulkRopewikiKmlImportRequest,
    inputs: dict[str, Any],
    progress_cb,
    job_id: str,
    **_: Any,
) -> dict[str, Any]:
    from .dispatcher import DispatchError, submit_tool_job  # avoid plugins ↔ dispatcher cycle

    bounds = req.bounds.tuple()
    container_id = req.container_dem_job_id
    progress_cb(f"query Ropewiki for canyons in {bounds} (limit {req.max_results})")
    candidates = list_ropewiki_canyons_in_bbox(bounds, max_results=req.max_results)
    progress_cb(f"Ropewiki returned {len(candidates)} candidate canyon pages")
    submitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    if not candidates:
        return {
            "bounds_wgs84": list(bounds),
            "container_dem_job_id": container_id,
            "candidate_count": 0,
            "submitted_jobs": submitted,
            "skipped": skipped,
        }

    parent_job = ctx.job_or_404(job_id)
    user = User(id=parent_job.created_by_user_id or "system", username="bulk-import")
    project_id = parent_job.project_id
    if not project_id:
        raise ValueError("bulk import requires an active project")

    for candidate in candidates:
        title = candidate["title"]
        source = ROPEWIKI_PAGE_URL.format(title=title.replace(" ", "_"))
        body: dict[str, Any] = {
            "params": {"source": source, "name": title, "container_dem_job_id": container_id},
            "inputs": {"container": container_id} if container_id else {},
        }
        try:
            child = submit_tool_job(ctx, "kml-import", body, user=user, project_id=project_id)
        except (DispatchError, ValueError) as exc:
            skipped.append({"page_title": title, "reason": str(exc)})
            progress_cb(f"skip {title}: {exc}")
            continue
        submitted.append(
            {
                "page_title": title,
                "job_id": child.id,
                "source": source,
                "latitude": candidate.get("latitude"),
                "longitude": candidate.get("longitude"),
            }
        )
        progress_cb(f"queued {title} -> job {child.id}")

    progress_cb(f"bulk import complete: {len(submitted)} queued, {len(skipped)} skipped")
    return {
        "bounds_wgs84": list(bounds),
        "container_dem_job_id": container_id,
        "candidate_count": len(candidates),
        "submitted_jobs": submitted,
        "skipped": skipped,
    }


def resync_kml_import(
    ctx: Any, *, job_id: str, source: dict[str, Any], kml_path: Path, progress_cb=None
) -> dict[str, Any]:
    """Re-fetch a kml-import's source URL and overwrite ``kml_path`` in place.

    Returns the rebuilt result dict from ``build_kml_import_result`` (without
    container assignment — callers preserve the existing container linkage).
    """
    if progress_cb is None:
        progress_cb = lambda _msg: None  # noqa: E731
    source_type = source.get("type")
    source_url = source.get("source")
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("kml-import has no source URL to sync from")

    display_name = (
        source.get("page_title") if isinstance(source.get("page_title"), str) else None
    ) or kml_path.stem

    if source_type == "ropewiki":
        ref = parse_ropewiki_ref(source_url)
        file_title, page_title = _find_kml_file_title(ref)
        page_url = (
            ROPEWIKI_PAGE_URL.format(title=page_title.replace(" ", "_"))
            if page_title
            else source_url
        )
        download_url = _get_kml_download_url(file_title) if file_title else None
        display = (page_title or display_name).strip() or display_name
        if file_title is None or download_url is None:
            skip_reason = (
                "no KML attachment"
                if file_title is None
                else f"attachment {file_title} has no download URL"
            )
            return _ropewiki_waypoint_result(
                ctx,
                ref=ref,
                source=source_url,
                page_title=page_title,
                page_url=page_url,
                kml_path=kml_path,
                display_name=display,
                progress_cb=progress_cb,
                skip_reason=skip_reason,
            )
        progress_cb(f"sync: download Ropewiki KML {file_title}")
        _download_file(download_url, kml_path)
        new_source = {
            "type": "ropewiki",
            "source": source_url,
            "page_title": page_title,
            "page_url": page_url,
            "file_title": file_title,
            "download_url": download_url,
        }
        return build_kml_import_result(ctx, kml_path, display, source=new_source)

    if source_type == "caltopo":
        fetch_url = caltopo_kml_url(source_url)
        kml_bytes = _fetch_caltopo_kml_bytes(
            source=source_url,
            fetch_url=fetch_url,
            document_name=display_name,
            progress_cb=progress_cb,
        )
        kml_path.write_bytes(kml_bytes)
        new_source: dict[str, Any] = {
            "type": "caltopo",
            "source": source_url,
            "download_url": fetch_url,
            "map_id": _caltopo_map_id(source_url) or "caltopo-map",
        }
        return build_kml_import_result(ctx, kml_path, display_name, source=new_source)

    if source_type == "url":
        progress_cb(f"sync: download KML from URL: {source_url}")
        content, resolved_url = _fetch_kml_following_network_link(
            source_url, progress_cb=progress_cb
        )
        if not content:
            raise ValueError(f"remote KML is empty: {source_url}")
        if not _looks_like_kml(content):
            raise ValueError(f"URL did not return KML content: {resolved_url}")
        kml_path.write_bytes(content)
        url_source: dict[str, Any] = {
            "type": "url",
            "source": source_url,
            "download_url": source_url,
        }
        if resolved_url != source_url:
            url_source["resolved_url"] = resolved_url
        return build_kml_import_result(ctx, kml_path, display_name, source=url_source)

    raise ValueError(f"kml-import has no syncable source type: {source_type!r}")


register(
    DemJobSpec(
        key="build-dem",
        label="Build DEM mosaic",
        description="Download cached TNM 1m DEM products for an AOI and write a cropped mosaic.",
        request_model=BuildDemToolRequest,
        visualizations=(),
        runner=_run_build_dem,
        runner_kind="context",
        inputs=(
            InputSpec(
                key="bounds", label="AOI bounds", kind="aoi_bounds", required=True, default="user"
            ),
        ),
        outputs=(
            OutputSpec(
                key="dem",
                label="DEM raster",
                path_field="dem_path",
                role="dem",
                asset_kind="raster",
                url_field="dem_url",
                asset_type="raster.dem",
            ),
        ),
        layers=(
            LayerSpec(
                key="dem_footprint",
                label="DEM footprint",
                kind="footprint",
                role="container",
                opacity=0.12,
            ),
        ),
    )
)


register(
    DemJobSpec(
        key="kml-import",
        label="Import Ropewiki KML",
        description="Import a canyon route KML from a Ropewiki page URL or title.",
        request_model=RopewikiKmlImportRequest,
        visualizations=(),
        runner=_run_ropewiki_kml_import,
        runner_kind="context",
        # No InputSpec for "container": KML imports are user-initiated from the
        # map popover, not run as a tool on a DEM asset. The container link is
        # still recorded via params._inputs.container.
        inputs=(),
        outputs=(
            OutputSpec(
                key="kml",
                label="Route KML",
                path_field="kml_path",
                role="route",
                asset_kind="vector",
                url_field="kml_url",
                asset_type="vector.kml.route",
            ),
        ),
        layers=(
            LayerSpec(
                key="route",
                label="Route",
                kind="vector-kml",
                url_field="kml_url",
                role="route",
            ),
        ),
    )
)


register(
    DemJobSpec(
        key="ropewiki-bulk-import",
        label="Import all Ropewiki canyons in AOI",
        description=(
            "Discover Ropewiki canyon pages whose coordinates fall inside the AOI "
            "(or the selected DEM container) and submit one Import KML job per canyon."
        ),
        request_model=BulkRopewikiKmlImportRequest,
        visualizations=(),
        runner=_run_bulk_ropewiki_kml_import,
        runner_kind="context",
        # No InputSpec: bulk import is map-popover driven, not run on a DEM row.
        inputs=(),
        outputs=(),
        layers=(),
    )
)
