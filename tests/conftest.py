"""Shared fixtures and helpers for the fuel route API test suite.

Geometry helpers build synthetic, perfectly straight east-west routes along a
fixed latitude so that route miles, station positions and expected costs can be
worked out by hand. All outbound HTTP is intercepted with `responses`, which also
lets tests assert exactly how many calls hit the routing servers.
"""
import itertools
import json
import math
import re
from decimal import Decimal
from pathlib import Path

import pytest
import responses
from django.core.cache import cache

FIXTURES = Path(__file__).parent / "fixtures"

EARTH_RADIUS_MI = 3958.8
ROUTE_LAT = 39.0
API_URL = "/api/v1/route-plan/"

OSRM_BASE = "https://osrm.test"
VALHALLA_BASE = "https://valhalla.test"
OSRM_ROUTE_URL = re.compile(r"^https://osrm\.test/route/v1/driving/")
VALHALLA_ROUTE_URL = re.compile(r"^https://valhalla\.test/route$")


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #
def haversine_miles(a, b):
    """a, b are (lat, lon) tuples."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(h))


def miles_per_degree_lon(lat=ROUTE_LAT):
    return haversine_miles((lat, 0.0), (lat, 1.0))


def lon_at_mile(mile, lon_start, lat=ROUTE_LAT):
    """Longitude that sits `mile` miles east of lon_start along the test latitude."""
    return lon_start + mile / miles_per_degree_lon(lat)


def straight_route(lon_start, lon_end, lat=ROUTE_LAT, points=400):
    """GeoJSON-ordered [lon, lat] coordinates for a straight east-west route."""
    step = (lon_end - lon_start) / (points - 1)
    return [[lon_start + i * step, lat] for i in range(points)]


def route_length_miles(coords):
    return sum(haversine_miles((a[1], a[0]), (b[1], b[0])) for a, b in zip(coords, coords[1:]))


def osrm_route_body(coords, distance_miles=None):
    """Mimic OSRM /route/v1/driving/... ?overview=full&geometries=geojson."""
    miles = distance_miles if distance_miles is not None else route_length_miles(coords)
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": {"type": "LineString", "coordinates": coords},
                "distance": miles * 1609.344,  # metres
                "duration": miles / 60 * 3600,  # seconds at 60 mph
                "legs": [],
            }
        ],
        "waypoints": [],
    }


def osrm_error_body(code, message="error"):
    return {"code": code, "message": message}


def encode_polyline6(latlon):
    out, plat, plon = [], 0, 0
    for lat, lon in latlon:
        for value, prev in ((round(lat * 1e6), plat), (round(lon * 1e6), plon)):
            d = value - prev
            d = ~(d << 1) if d < 0 else d << 1
            while d >= 0x20:
                out.append(chr((0x20 | (d & 0x1F)) + 63))
                d >>= 5
            out.append(chr(d + 63))
        plat, plon = round(lat * 1e6), round(lon * 1e6)
    return "".join(out)


def valhalla_route_body(coords):
    """Mimic Valhalla /route with an encoded polyline6 shape (its default)."""
    latlon = [(c[1], c[0]) for c in coords]
    miles = route_length_miles(coords)
    return {
        "trip": {
            "legs": [{"shape": encode_polyline6(latlon)}],
            "summary": {"length": miles, "time": miles / 60 * 3600},
            "status": 0,
        }
    }


def routing_calls(rsps):
    return [c for c in rsps.calls if OSRM_ROUTE_URL.match(c.request.url) or VALHALLA_ROUTE_URL.match(c.request.url)]


def osrm_calls(rsps):
    return [c for c in rsps.calls if OSRM_ROUTE_URL.match(c.request.url)]


def valhalla_calls(rsps):
    return [c for c in rsps.calls if VALHALLA_ROUTE_URL.match(c.request.url)]


def dec(value):
    return Decimal(str(value))


def pt(lat, lon):
    return {"lat": lat, "lon": lon}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolated_settings(settings):
    """Deterministic settings and a clean cache/index for every test."""
    from trips.services.station_index import StationIndex

    settings.ROUTING_PROVIDERS = ["osrm", "valhalla"]
    settings.OSRM_BASE_URL = OSRM_BASE
    settings.VALHALLA_BASE_URL = VALHALLA_BASE
    settings.OSRM_PROFILE = "driving"
    settings.ROUTING_TIMEOUT_SECONDS = 5
    settings.FUEL_CORRIDOR_MILES = 10
    settings.VEHICLE_RANGE_MILES = 500
    settings.VEHICLE_MPG = 10
    settings.STOP_PENALTY_USD = 0  # hand-worked expectations use the pure cheapest plan
    settings.GAZETTEER_PATH = FIXTURES / "gazetteer_sample.csv"
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    cache.clear()
    StationIndex.reset()
    yield
    cache.clear()
    StationIndex.reset()


@pytest.fixture
def mocked_http():
    """Every outbound request must be explicitly mocked; unmocked calls fail loudly."""
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        yield rsps


@pytest.fixture
def mock_route(mocked_http):
    """Register an OSRM response for a straight route. Returns the coords."""

    def _register(lon_start, lon_end, lat=ROUTE_LAT, distance_miles=None, coords=None):
        coords = coords or straight_route(lon_start, lon_end, lat)
        mocked_http.add(responses.GET, OSRM_ROUTE_URL, json=osrm_route_body(coords, distance_miles), status=200)
        return coords

    return _register


@pytest.fixture
def make_station(db):
    """Create a geocoded FuelStation placed at a given route mile (or explicit lat/lon)."""
    from trips.models import FuelStation

    ids = itertools.count(900_000)

    def _make(*, price, mile=None, lon_start=-104.0, lat=ROUTE_LAT, lon=None, north_offset_miles=0.0,
              state="KS", name=None, opis_id=None):
        if lon is None:
            assert mile is not None, "give either mile= or lon="
            lon = lon_at_mile(mile, lon_start, lat)
        lat = lat + north_offset_miles / 69.0
        opis_id = opis_id or next(ids)
        return FuelStation.objects.create(
            opis_id=opis_id,
            name=name or f"TEST STOP {opis_id}",
            address="I-70, EXIT 1",
            city="Testville",
            state=state,
            rack_id=1,
            retail_price=dec(price),
            lat=lat,
            lon=lon,
            geocode_source="test",
        )

    return _make


@pytest.fixture
def plan(client):
    """POST a route-plan request and return the response."""

    def _plan(start, finish, **extra):
        body = {"start": start, "finish": finish, **extra}
        return client.post(API_URL, data=json.dumps(body), content_type="application/json")

    return _plan
