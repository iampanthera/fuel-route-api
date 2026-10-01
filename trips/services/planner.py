"""Orchestrates one trip plan:

  resolve locations -> service-area check -> route (cache or 1 provider call)
  -> stations in corridor -> optimiser -> response body (+ stored for the map page)
"""
import hashlib
import time
import uuid
from decimal import Decimal

import numpy as np
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse
import shapely

from .errors import InfeasibleRoute
from .gazetteer import get_gazetteer
from .geo import cumulative_miles, downsample, haversine_miles
from .locations import ensure_in_service_area, resolve_location
from .optimizer import InfeasibleRouteError, RouteStop, plan_fuel_stops
from .routing import get_route
from .station_index import StationIndex

SAME_POINT_MILES = 0.1
GEOMETRY_TOLERANCE_DEG = 0.0005  # about 50 m; keeps the map faithful, shrinks the payload


def _route_key(start, end):
    raw = f"{start.lat:.4f},{start.lon:.4f};{end.lat:.4f},{end.lon:.4f}|{','.join(settings.ROUTING_PROVIDERS)}"
    return "route:v1:" + hashlib.sha1(raw.encode()).hexdigest()


def _plan_id(route_key):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, route_key))


def _money(d: Decimal) -> str:
    return str(d.quantize(Decimal("0.01")))


def _assumptions(stop_penalty):
    tank = settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG
    return [
        f"Vehicle starts with a full, already-paid {tank:g} gal tank; total_fuel_cost counts fuel bought "
        "on the route, trip_fuel_cost_estimate also prices the starting tank at the route's median price",
        "Vehicle arrives at the destination with an empty tank",
        "Station locations are city-level; detour distance to a station is shown but not charged",
        "Duplicate price rows for one station use the lowest price",
        "Fuel stops are optimised along the fastest route returned by the routing server",
        f"Each extra stop is weighed as ${stop_penalty:g} when choosing stops (never added to the cost); "
        "0 means pure cheapest fuel",
    ]


def _vehicle():
    return {
        "range_miles": round(settings.VEHICLE_RANGE_MILES, 1),
        "mpg": round(settings.VEHICLE_MPG, 1),
        "tank_gallons": round(settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG, 1),
    }


def _simplify(lats, lons):
    coords = np.column_stack([lons, lats])
    if len(coords) > 2:
        line = shapely.simplify(shapely.linestrings(coords), GEOMETRY_TOLERANCE_DEG, preserve_topology=False)
        coords = shapely.get_coordinates(line)
    return {"type": "LineString", "coordinates": np.round(coords, 5).tolist()}


def _point_at_mile(lats, lons, cum, mile):
    i = int(np.clip(np.searchsorted(cum, mile), 1, len(cum) - 1))
    span = cum[i] - cum[i - 1]
    t = 0.0 if span <= 0 else (mile - cum[i - 1]) / span
    return float(lats[i - 1] + t * (lats[i] - lats[i - 1])), float(lons[i - 1] + t * (lons[i] - lons[i - 1]))


def plan_trip(start_raw, finish_raw, stop_penalty=None) -> dict:
    t0 = time.perf_counter()
    if stop_penalty is None:
        stop_penalty = settings.STOP_PENALTY_USD
    start = resolve_location(start_raw, "start")
    finish = resolve_location(finish_raw, "finish")
    ensure_in_service_area(start, "start")
    ensure_in_service_area(finish, "finish")

    key = _route_key(start, finish)
    plan_id = _plan_id(f"{key}|penalty={stop_penalty:g}")
    routing_calls, cache_hit = 0, False

    if haversine_miles(start.lat, start.lon, finish.lat, finish.lon) < SAME_POINT_MILES:
        route = {"lats": np.array([start.lat, finish.lat]), "lons": np.array([start.lon, finish.lon]),
                 "distance_miles": 0.0, "duration_seconds": 0.0, "provider": "none"}
    else:
        route = cache.get(key)
        if route is not None:
            cache_hit = True
        else:
            r = get_route((start.lat, start.lon), (finish.lat, finish.lon))
            routing_calls = r.calls
            route = {"lats": np.array([p[0] for p in r.latlon]), "lons": np.array([p[1] for p in r.latlon]),
                     "distance_miles": r.distance_miles, "duration_seconds": r.duration_seconds,
                     "provider": r.provider}
            cache.set(key, route, settings.ROUTE_CACHE_SECONDS)

    lats, lons = route["lats"], route["lons"]
    total_miles = float(route["distance_miles"])
    cum = cumulative_miles(lats, lons, total_miles if total_miles > 0 else None)

    corridor = []
    if total_miles > 0:  # also for short trips: their prices value the starting tank
        m_lats, m_lons, m_cum = downsample(lats, lons, cum)
        corridor = StationIndex.get().stations_along(m_lats, m_lons, m_cum, settings.FUEL_CORRIDOR_MILES)
    by_id = {c.station.opis_id: c for c in corridor}

    try:
        plan = plan_fuel_stops(
            [RouteStop(c.station.opis_id, c.route_mile, c.station.retail_price) for c in corridor],
            total_miles,
            max_range_miles=settings.VEHICLE_RANGE_MILES,
            mpg=settings.VEHICLE_MPG,
            stop_penalty=stop_penalty,
        )
    except InfeasibleRouteError as exc:
        gaz = get_gazetteer()
        lat_a, lon_a = _point_at_mile(lats, lons, cum, exc.from_mile)
        lat_b, lon_b = _point_at_mile(lats, lons, cum, exc.to_mile)
        pa, pb = gaz.nearest(lat_a, lon_a), gaz.nearest(lat_b, lon_b)
        raise InfeasibleRoute(
            f"No fuel station within {settings.VEHICLE_RANGE_MILES:g} miles between route mile "
            f"{exc.from_mile:.0f} and {exc.to_mile:.0f}; the price list has no stations on that stretch.",
            {
                "from_mile": round(exc.from_mile, 1),
                "to_mile": round(exc.to_mile, 1),
                "gap_miles": round(exc.to_mile - exc.from_mile, 1),
                "near": {
                    "from": {"lat": round(lat_a, 4), "lon": round(lon_a, 4), "place": f"{pa.city}, {pa.state}" if pa else None},
                    "to": {"lat": round(lat_b, 4), "lon": round(lon_b, 4), "place": f"{pb.city}, {pb.state}" if pb else None},
                },
                "route_distance_miles": round(total_miles, 1),
            },
        ) from None

    stops = []
    for seq, s in enumerate(plan.stops, start=1):
        c = by_id[s.station_id]
        st = c.station
        stops.append({
            "sequence": seq,
            "station_id": st.opis_id,
            "name": st.name,
            "address": st.address,
            "city": st.city,
            "state": st.state,
            "lat": st.lat,
            "lon": st.lon,
            "route_mile": round(s.route_mile, 1),
            "offset_miles": round(c.offset_miles, 1),
            "price_per_gallon": str(s.price.quantize(Decimal("0.001"))),
            "gallons": str(s.gallons),
            "cost": _money(s.cost),
        })

    # The optimiser treats the starting tank as already paid, so a short trip
    # costs $0. Also report what all the fuel burned on the trip is worth, with
    # the starting-tank share priced at the median corridor price.
    tank = Decimal(str(settings.VEHICLE_RANGE_MILES / settings.VEHICLE_MPG))
    # capped: the DP rounds legs to 0.01 gal, so purchases can trail consumption by a hair
    tank_gallons_used = min(tank, max(Decimal("0"), plan.gallons_consumed - plan.gallons_purchased))
    ref_price = (
        Decimal(str(np.median([float(c.station.retail_price) for c in corridor]))).quantize(Decimal("0.001"))
        if corridor else None
    )
    trip_estimate = (
        _money(plan.total_cost + tank_gallons_used * ref_price) if ref_price is not None
        else (_money(plan.total_cost) if tank_gallons_used == 0 else None)
    )

    body = {
        "plan_id": plan_id,
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "route": {
            "distance_miles": round(total_miles, 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 2),
            "provider": route["provider"],
            "geometry": _simplify(lats, lons),
        },
        "fuel_stops": stops,
        "summary": {
            "total_fuel_cost": _money(plan.total_cost),
            "gallons_purchased": str(plan.gallons_purchased),
            "gallons_consumed": str(plan.gallons_consumed),
            "starting_tank_gallons_used": str(tank_gallons_used.quantize(Decimal("0.001"))),
            "route_median_price_per_gallon": str(ref_price) if ref_price is not None else None,
            "trip_fuel_cost_estimate": trip_estimate,
            "stations_considered": len(corridor),
            "vehicle": _vehicle(),
            "assumptions": _assumptions(stop_penalty),
            "stop_penalty_usd": stop_penalty,
        },
        "map_url": reverse("route-map", kwargs={"plan_id": plan_id}),
    }
    cache.set(f"plan:{plan_id}", body, settings.ROUTE_CACHE_SECONDS)
    body = {**body, "meta": {
        "routing_api_calls": routing_calls,
        "geocoding_api_calls": 0,
        "cache_hit": cache_hit,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
    }}
    return body


def get_stored_plan(plan_id: str):
    return cache.get(f"plan:{plan_id}")
