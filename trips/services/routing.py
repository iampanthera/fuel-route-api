"""Routing providers: OSRM (primary) and Valhalla (fallback).

Both are open-source engines with free public servers hosted by FOSSGIS, and
neither needs an API key. A normal request makes exactly one call. The fallback
is only tried when the primary fails in a way another server could fix
(timeout, 5xx, rate limit, malformed reply); "no route exists" is final.
"""
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

from .errors import (
    NoRoute,
    RouteTooLong,
    RoutingError,
    RoutingProviderError,
    RoutingRateLimited,
    RoutingTimeout,
)
from .geo import decode_polyline

log = logging.getLogger("trips.routing")
METERS_PER_MILE = 1609.344


@dataclass
class Route:
    latlon: list  # [(lat, lon), ...]
    distance_miles: float
    duration_seconds: float
    provider: str
    calls: int = 1
    attempts: list = field(default_factory=list)


def _session():
    s = requests.Session()
    s.headers["User-Agent"] = settings.ROUTING_USER_AGENT
    return s


def _classify_http(provider, exc_or_resp):
    """Turn transport-level failures into typed errors (never echoing URLs)."""
    if isinstance(exc_or_resp, requests.Timeout):
        return RoutingTimeout(f"{provider} did not respond in time")
    if isinstance(exc_or_resp, requests.RequestException):
        return RoutingProviderError(f"Could not reach {provider}")
    resp = exc_or_resp
    if resp.status_code == 429:
        headers = {"Retry-After": resp.headers["Retry-After"]} if "Retry-After" in resp.headers else {}
        return RoutingRateLimited(f"{provider} rate limit reached", headers=headers)
    return RoutingProviderError(f"{provider} returned HTTP {resp.status_code}")


class OsrmClient:
    name = "osrm"

    def route(self, start, end) -> Route:
        (lat1, lon1), (lat2, lon2) = start, end
        url = (
            f"{settings.OSRM_BASE_URL.rstrip('/')}/route/v1/{settings.OSRM_PROFILE}/"
            f"{lon1:.6f},{lat1:.6f};{lon2:.6f},{lat2:.6f}"
        )
        params = {"overview": "full", "geometries": "geojson", "steps": "false", "alternatives": "false"}
        try:
            resp = _session().get(url, params=params, timeout=settings.ROUTING_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise _classify_http(self.name, exc) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        code = body.get("code") if isinstance(body, dict) else None
        if code in ("NoRoute", "NoSegment"):
            raise NoRoute("No drivable route between these points", details={"provider_code": code})
        if code == "TooBig":
            raise RouteTooLong("Route request too large for the routing server")
        if resp.status_code != 200 or code != "Ok":
            if resp.status_code == 200:
                raise RoutingProviderError(f"{self.name} returned an unexpected response")
            raise _classify_http(self.name, resp)
        try:
            r = body["routes"][0]
            coords = r["geometry"]["coordinates"]
            latlon = [(float(c[1]), float(c[0])) for c in coords]
            if len(latlon) < 2:
                raise ValueError("empty geometry")
            return Route(latlon, float(r["distance"]) / METERS_PER_MILE, float(r["duration"]), self.name)
        except (KeyError, IndexError, TypeError, ValueError):
            raise RoutingProviderError(f"{self.name} returned an unexpected response") from None


class ValhallaClient:
    name = "valhalla"
    NO_ROUTE_CODES = {170, 171, 442, 443}
    TOO_LONG_CODES = {154}

    def route(self, start, end) -> Route:
        (lat1, lon1), (lat2, lon2) = start, end
        payload = {
            "locations": [{"lat": lat1, "lon": lon1}, {"lat": lat2, "lon": lon2}],
            "costing": settings.VALHALLA_COSTING,
            "units": "miles",
            "directions_type": "none",
        }
        url = f"{settings.VALHALLA_BASE_URL.rstrip('/')}/route"
        try:
            resp = _session().post(url, json=payload, timeout=settings.ROUTING_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise _classify_http(self.name, exc) from None
        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200:
            err = body.get("error_code") if isinstance(body, dict) else None
            if err in self.NO_ROUTE_CODES:
                raise NoRoute("No drivable route between these points", details={"provider_code": err})
            if err in self.TOO_LONG_CODES:
                raise RouteTooLong("Route is longer than the routing server allows")
            raise _classify_http(self.name, resp)
        try:
            trip = body["trip"]
            latlon = []
            for leg in trip["legs"]:
                shape = leg["shape"]
                pts = (
                    [(c[1], c[0]) for c in shape["coordinates"]] if isinstance(shape, dict) else decode_polyline(shape, 6)
                )
                latlon.extend(pts if not latlon else pts[1:])
            if len(latlon) < 2:
                raise ValueError("empty geometry")
            return Route(latlon, float(trip["summary"]["length"]), float(trip["summary"]["time"]), self.name)
        except (KeyError, IndexError, TypeError, ValueError):
            raise RoutingProviderError(f"{self.name} returned an unexpected response") from None


PROVIDERS = {"osrm": OsrmClient, "valhalla": ValhallaClient}


def get_route(start, end) -> Route:
    """Try providers in order; fall back only on retryable failures."""
    names = [n.strip() for n in settings.ROUTING_PROVIDERS if n.strip()]
    attempts, first_error = [], None
    for name in names:
        client = PROVIDERS[name]()
        try:
            route = client.route(start, end)
            route.calls = len(attempts) + 1
            route.attempts = attempts + [{"provider": name, "ok": True}]
            return route
        except RoutingError as err:
            log.warning("Routing via %s failed: %s", name, err.code)
            attempts.append({"provider": name, "ok": False, "error": err.code})
            first_error = first_error or err
            if not err.retryable:
                err.details = {**err.details, "attempts": attempts}
                raise err
    first_error.details = {**first_error.details, "attempts": attempts}
    raise first_error
