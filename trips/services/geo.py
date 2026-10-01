"""Geometry helpers: distances, route mile markers, station-to-route projection,
and the lower-48 service-area check. All distances are statute miles."""
import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings
from shapely.geometry import Point, shape
from shapely.prepared import prep

EARTH_RADIUS_MI = 3958.8
MILES_PER_DEG_LAT = 69.05


def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance; works on scalars or numpy arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MI * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0)))


def cumulative_miles(lats: np.ndarray, lons: np.ndarray, provider_miles: float | None = None) -> np.ndarray:
    """Mile marker at each vertex. Scaled so the last marker equals the provider's
    reported distance (road distance and polyline length differ slightly)."""
    if len(lats) < 2:
        return np.zeros(len(lats))
    seg = haversine_miles(lats[:-1], lons[:-1], lats[1:], lons[1:])
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    if provider_miles and cum[-1] > 0:
        cum *= provider_miles / cum[-1]
    return cum


def downsample(lats, lons, cum, min_spacing_miles=0.5):
    """Keep one vertex per `min_spacing_miles` of route (plus both ends).
    OSRM polylines can hold tens of thousands of points; this keeps projection
    cheap without moving any vertex."""
    if len(cum) <= 2:
        return lats, lons, cum
    buckets = np.floor(cum / min_spacing_miles)
    keep = np.nonzero(np.diff(buckets, prepend=-1) > 0)[0]
    if keep[-1] != len(cum) - 1:
        keep = np.append(keep, len(cum) - 1)
    return lats[keep], lons[keep], cum[keep]


def project_points_on_route(route_lats, route_lons, route_cum, pts_lats, pts_lons, corridor_miles, chunk=128):
    """For each point, find the route mile of its closest approach and the offset.

    Returns (route_mile, offset_miles) arrays; offset is +inf for points further
    than `corridor_miles` from the route. When a route passes the same point
    twice (out-and-back), the earliest pass within 0.1 mi of the best offset wins.

    Distances use a local equirectangular projection around each point, accurate
    to well under 1% at corridor scale. Two vectorised stages keep it fast:
    a coarse vertex-distance filter, then exact point-to-segment distances for
    the survivors only.
    """
    n_pts = len(pts_lats)
    route_mile = np.full(n_pts, np.nan)
    offset = np.full(n_pts, np.inf)
    if n_pts == 0 or len(route_lats) < 2:
        return route_mile, offset

    pts_lats = np.asarray(pts_lats, dtype=float)
    pts_lons = np.asarray(pts_lons, dtype=float)
    kx = np.cos(np.radians(pts_lats))[:, None] * MILES_PER_DEG_LAT

    # Stage 1: distance to vertices of a coarse copy of the route (every ~5 mi).
    c_lats, c_lons, _ = downsample(route_lats, route_lons, route_cum, 5.0)
    survivors, order = [], []
    for lo in range(0, n_pts, chunk):
        sl = slice(lo, lo + chunk)
        dx = (c_lons[None, :] - pts_lons[sl, None]) * kx[sl]
        dy = (c_lats[None, :] - pts_lats[sl, None]) * MILES_PER_DEG_LAT
        dist = np.hypot(dx, dy)
        near = np.nonzero(dist.min(axis=1) <= corridor_miles + 5.0)[0]
        survivors.extend(near + lo)
        order.extend(dist[near].argmin(axis=1))
    if not survivors:
        return route_mile, offset
    # Group survivors that sit close together along the route, so each chunk
    # only needs the segments inside its own small bounding box.
    surv = np.array(survivors)[np.argsort(order, kind="stable")]
    chunk = 32

    # Stage 2: exact distance from each survivor to every fine segment.
    a_lat, a_lon = route_lats[:-1], route_lons[:-1]
    d_lat, d_lon = route_lats[1:] - a_lat, route_lons[1:] - a_lon
    seg_len = route_cum[1:] - route_cum[:-1]
    seg_lat_lo, seg_lat_hi = np.minimum(a_lat, route_lats[1:]), np.maximum(a_lat, route_lats[1:])
    seg_lon_lo, seg_lon_hi = np.minimum(a_lon, route_lons[1:]), np.maximum(a_lon, route_lons[1:])
    pad_lat = (corridor_miles + 1.0) / MILES_PER_DEG_LAT
    for lo in range(0, len(surv), chunk):
        ids = surv[lo : lo + chunk]
        plats, plons = pts_lats[ids], pts_lons[ids]
        pad_lon = (corridor_miles + 1.0) / (np.cos(np.radians(np.abs(plats).max())) * MILES_PER_DEG_LAT)
        segs = np.nonzero(
            (seg_lat_hi >= plats.min() - pad_lat) & (seg_lat_lo <= plats.max() + pad_lat)
            & (seg_lon_hi >= plons.min() - pad_lon) & (seg_lon_lo <= plons.max() + pad_lon)
        )[0]
        if len(segs) == 0:
            continue
        k = kx[ids]
        ax = (a_lon[None, segs] - plons[:, None]) * k
        ay = (a_lat[None, segs] - plats[:, None]) * MILES_PER_DEG_LAT
        dx = d_lon[None, segs] * k
        dy = np.broadcast_to(d_lat[None, segs] * MILES_PER_DEG_LAT, ax.shape)
        denom = dx * dx + dy * dy
        with np.errstate(invalid="ignore", divide="ignore"):
            t = np.where(denom > 0, -(ax * dx + ay * dy) / denom, 0.0)
        np.clip(t, 0.0, 1.0, out=t)
        d = np.hypot(ax + t * dx, ay + t * dy)
        best = d.min(axis=1)
        # earliest segment whose distance is within 0.1 mi of the best one
        first = np.argmax(d <= best[:, None] + 0.1, axis=1)
        ok = best <= corridor_miles
        rows = np.nonzero(ok)[0]
        col = first[rows]
        seg = segs[col]
        offset[ids[rows]] = d[rows, col]
        route_mile[ids[rows]] = route_cum[seg] + t[rows, col] * seg_len[seg]
    return route_mile, offset


@lru_cache(maxsize=2)
def _service_area(path: str):
    with open(path) as fh:
        data = json.load(fh)
    geom = shape(data["geometry"] if data.get("type") == "Feature" else data)
    return prep(geom)


def in_service_area(lat: float, lon: float) -> bool:
    return _service_area(str(Path(settings.SERVICE_AREA_PATH))).contains(Point(lon, lat))


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Decode a Google/Valhalla encoded polyline into [(lat, lon), ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10**precision
    while index < len(encoded):
        for which in (0, 1):
            result, shift = 0, 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords
