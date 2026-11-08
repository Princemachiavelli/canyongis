"""GeoPackage geometry blobs (GPKG header + little-endian WKB).

Only the geometry types the canyon schema uses are encoded here. The header
carries no envelope, so the blob is fully determined by the coordinates.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence

WGS84_SRS_ID = 4326

_WKB_POINT = 1
_WKB_LINESTRING = 2
# Header flags: bit 0 = little-endian, bits 1-3 = envelope (0 = none).
_HEADER_FLAGS_LE_NO_ENVELOPE = 0b0000_0001


def _gpkg_header(srs_id: int) -> bytes:
    return b"GP" + bytes([0, _HEADER_FLAGS_LE_NO_ENVELOPE]) + struct.pack("<i", srs_id)


def point_blob(lon: float, lat: float, srs_id: int = WGS84_SRS_ID) -> bytes:
    wkb = struct.pack("<BIdd", 1, _WKB_POINT, float(lon), float(lat))
    return _gpkg_header(srs_id) + wkb


def linestring_blob(coords: Sequence[tuple[float, float]], srs_id: int = WGS84_SRS_ID) -> bytes:
    if len(coords) < 2:
        raise ValueError("LineString needs at least two coordinates")
    wkb = struct.pack("<BII", 1, _WKB_LINESTRING, len(coords))
    wkb += b"".join(struct.pack("<dd", float(x), float(y)) for x, y in coords)
    return _gpkg_header(srs_id) + wkb


def blob_envelope(blob: bytes) -> tuple[float, float, float, float] | None:
    """Return (min_x, min_y, max_x, max_y) for a blob written by this module."""
    if blob is None:
        return None
    if blob[:2] != b"GP":
        raise ValueError("not a GeoPackage geometry blob")
    flags = blob[3]
    if flags & 0b0000_0001 == 0:
        raise ValueError("big-endian GeoPackage blobs are not supported")
    wkb_type = struct.unpack_from("<I", blob, 8 + 1)[0]
    if wkb_type == _WKB_POINT:
        x, y = struct.unpack_from("<dd", blob, 8 + 5)
        return (x, y, x, y)
    if wkb_type == _WKB_LINESTRING:
        n = struct.unpack_from("<I", blob, 8 + 5)[0]
        xs = struct.unpack_from(f"<{2 * n:d}d", blob, 8 + 9)
        pts_x, pts_y = xs[0::2], xs[1::2]
        return (min(pts_x), min(pts_y), max(pts_x), max(pts_y))
    raise ValueError(f"unsupported WKB type {wkb_type}")
