"""Helpers for extracting and registering asset rows from job results.

An "asset" is a single output file created by a job. We infer a kind from the
file extension and the result-dict key that referenced it. The same path may
appear under multiple keys (e.g. ``dem_path`` and ``hillshade_tif_path``) — we
de-dupe by absolute path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PATH_KEY_SUFFIXES = ("_path",)
PATH_KEY_NAMES = {"out_dir", "kml_path", "dem_path"}
URL_SUFFIX = "_url"

EXTENSION_KIND = {
    ".tif": "tif",
    ".tiff": "tif",
    ".kml": "kml",
    ".kmz": "kml",
    ".gpkg": "gpkg",
    ".pmtiles": "pmtiles",
    ".png": "preview_png",
    ".webp": "preview_webp",
    ".jpg": "preview_jpg",
    ".jpeg": "preview_jpg",
    ".geojson": "geojson",
    ".json": "json",
}

EXTENSION_MEDIA_TYPE = {
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".kml": "application/vnd.google-earth.kml+xml",
    ".kmz": "application/vnd.google-earth.kmz",
    ".gpkg": "application/geopackage+sqlite3",
    ".pmtiles": "application/vnd.pmtiles",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".geojson": "application/geo+json",
    ".json": "application/json",
}


@dataclass(frozen=True)
class AssetCandidate:
    path: str
    kind: str
    url: str | None
    format: str | None
    media_type: str | None
    metadata: dict[str, Any]


def _is_path_key(key: str) -> bool:
    return key in PATH_KEY_NAMES or key.endswith(PATH_KEY_SUFFIXES)


def _url_for(result: dict[str, Any], path_key: str) -> str | None:
    if path_key in PATH_KEY_NAMES:
        candidates = [f"{Path(path_key).stem}{URL_SUFFIX}", path_key.replace("_path", URL_SUFFIX)]
    else:
        candidates = [path_key.replace("_path", URL_SUFFIX)]
    for k in candidates:
        v = result.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def extract_asset_candidates(
    result: dict[str, Any] | None,
    *,
    output_root: Path,
    require_inside_root: bool = True,
) -> list[AssetCandidate]:
    """Walk a job ``result`` dict and return one AssetCandidate per unique file.

    Only includes paths that exist on disk. When ``require_inside_root`` is true
    (the default), excludes paths outside ``output_root`` — this prevents
    registering references to externally-cached DEM tiles, etc.
    """
    if not result:
        return []
    root = output_root.resolve()
    bounds = result.get("bounds_wgs84") if isinstance(result.get("bounds_wgs84"), list) else None

    seen: dict[str, AssetCandidate] = {}
    for key, value in result.items():
        if not isinstance(value, str) or not value:
            continue
        if not _is_path_key(key):
            continue
        path = Path(value)
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        if require_inside_root:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
        key_str = str(resolved)
        if key_str in seen:
            continue
        ext = resolved.suffix.lower()
        kind = EXTENSION_KIND.get(ext, "file")
        metadata: dict[str, Any] = {"result_key": key}
        if bounds is not None:
            metadata["bounds_wgs84"] = bounds
        seen[key_str] = AssetCandidate(
            path=key_str,
            kind=kind,
            url=_url_for(result, key),
            format=ext.lstrip(".") or None,
            media_type=EXTENSION_MEDIA_TYPE.get(ext),
            metadata=metadata,
        )
    return list(seen.values())


def new_asset_id() -> str:
    return uuid.uuid4().hex[:16]
