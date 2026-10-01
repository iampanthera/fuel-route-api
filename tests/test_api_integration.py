"""End-to-end integration tests: HTTP request -> view -> planner -> DB -> mocked routing.

Routing servers (OSRM primary, Valhalla fallback) are mocked with `responses`,
so every test can also assert how many external calls were made (a hard
requirement in the brief).

Standard synthetic trip: a straight line along lat 39.0 from lon -104 to lon -86
(about 968 mi). Stations are placed by route mile with `make_station(mile=...)`.
"""
import json
import os
import time
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from .conftest import (
    API_URL,
    OSRM_ROUTE_URL,
    ROUTE_LAT,
    VALHALLA_ROUTE_URL,
    lon_at_mile,
    osrm_calls,
    osrm_error_body,
    osrm_route_body,
    pt,
    route_length_miles,
    routing_calls,
    straight_route,
    valhalla_calls,
    valhalla_route_body,
)

pytestmark = pytest.mark.django_db

LON_START, LON_END = -104.0, -86.0
START, FINISH = pt(ROUTE_LAT, LON_START), pt(ROUTE_LAT, LON_END)


@pytest.fixture
def cross_state_stations(make_station):
    """Stations for the standard ~968 mi trip. Hand-worked optimum: S2 then S4."""
    return {
        "S1": make_station(mile=150, price=3.50),
        "S2": make_station(mile=420, price=3.10),
        "S3": make_station(mile=480, price=3.90),
        "S4": make_station(mile=700, price=2.90),
        "S5": make_station(mile=900, price=3.40),
        # Cheapest of all, but 60 mi north of the route -> must be ignored.
        "OFF": make_station(mile=450, price=1.99, north_offset_miles=60),
    }


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_short_trip_returns_route_and_no_stops(plan, mock_route, make_station, mocked_http):
    make_station(mile=100, price=3.00)
    mock_route(LON_START, -99.0)  # about 269 mi

    resp = plan(START, pt(ROUTE_LAT, -99.0))

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["fuel_stops"] == []
    assert body["summary"]["total_fuel_cost"] == "0.00"
    assert body["route"]["distance_miles"] == pytest.approx(269, abs=2)
    assert body["route"]["geometry"]["type"] == "LineString"
    assert len(body["route"]["geometry"]["coordinates"]) >= 2
    assert body["route"]["provider"] == "osrm"
    assert body["meta"]["routing_api_calls"] == 1
    assert len(routing_calls(mocked_http)) == 1


def test_long_trip_picks_optimal_stops_and_total(plan, mock_route, cross_state_stations):
    coords = mock_route(LON_START, LON_END)
    total_miles = route_length_miles(coords)

    resp = plan(START, FINISH)

    assert resp.status_code == 200, resp.content
    body = resp.json()
    stops = body["fuel_stops"]
    assert [s["station_id"] for s in stops] == [
        cross_state_stations["S2"].opis_id,
        cross_state_stations["S4"].opis_id,
    ]
    assert [s["sequence"] for s in stops] == [1, 2]

    # S2: arrive with 8 gal, buy just enough (20) to reach cheaper S4 at mile 700.
    assert float(stops[0]["gallons"]) == pytest.approx(20.0, abs=0.2)
    # S4: buy exactly enough to finish empty.
    assert float(stops[1]["gallons"]) == pytest.approx((total_miles - 700) / 10, abs=0.2)

    expected = 20 * 3.10 + (total_miles - 700) / 10 * 2.90
    assert float(body["summary"]["total_fuel_cost"]) == pytest.approx(expected, abs=1.0)
    assert float(body["summary"]["gallons_consumed"]) == pytest.approx(total_miles / 10, abs=0.5)


def test_response_money_fields_are_consistent(plan, mock_route, cross_state_stations):
    mock_route(LON_START, LON_END)
    body = plan(START, FINISH).json()

    total = Decimal(body["summary"]["total_fuel_cost"])
    assert total == sum(Decimal(s["cost"]) for s in body["fuel_stops"])
    assert total == total.quantize(Decimal("0.01"))
    for s in body["fuel_stops"]:
        assert isinstance(s["cost"], str) and isinstance(s["price_per_gallon"], str)
        assert Decimal(s["cost"]) == (Decimal(s["price_per_gallon"]) * Decimal(s["gallons"])).quantize(Decimal("0.01"))
    assert body["summary"]["vehicle"] == {"range_miles": 500, "mpg": 10, "tank_gallons": 50}
    assert body["summary"]["assumptions"], "assumptions must be surfaced to the caller"


def test_short_trip_reports_trip_cost_estimate(plan, mock_route, make_station):
    # Optimiser cost is $0 (starting tank covers it); the estimate prices the
    # burned fuel at the median corridor price.
    make_station(mile=50, price=3.00)
    make_station(mile=100, price=3.20)
    make_station(mile=150, price=3.40)
    mock_route(LON_START, -99.0)

    s = plan(START, pt(ROUTE_LAT, -99.0)).json()["summary"]

    assert s["total_fuel_cost"] == "0.00"
    assert s["route_median_price_per_gallon"] == "3.200"
    assert s["starting_tank_gallons_used"] == s["gallons_consumed"]
    assert Decimal(s["trip_fuel_cost_estimate"]) == (Decimal(s["gallons_consumed"]) * Decimal("3.2")).quantize(Decimal("0.01"))


def test_long_trip_estimate_adds_starting_tank(plan, mock_route, cross_state_stations):
    mock_route(LON_START, LON_END)
    s = plan(START, FINISH).json()["summary"]

    tank_used = min(Decimal(50), Decimal(s["gallons_consumed"]) - Decimal(s["gallons_purchased"]))
    assert Decimal(s["starting_tank_gallons_used"]) == tank_used
    expected = Decimal(s["total_fuel_cost"]) + tank_used * Decimal(s["route_median_price_per_gallon"])
    assert Decimal(s["trip_fuel_cost_estimate"]) == expected.quantize(Decimal("0.01"))


def test_estimate_is_null_when_no_station_prices_the_tank(plan, mock_route, mocked_http):
    mock_route(LON_START, -99.0)
    s = plan(START, pt(ROUTE_LAT, -99.0)).json()["summary"]
    assert s["route_median_price_per_gallon"] is None
    assert s["trip_fuel_cost_estimate"] is None


def test_no_leg_exceeds_vehicle_range(plan, mock_route, cross_state_stations):
    coords = mock_route(LON_START, LON_END)
    body = plan(START, FINISH).json()
    marks = [0.0] + [s["route_mile"] for s in body["fuel_stops"]] + [route_length_miles(coords)]
    assert all(b - a <= 500 + 0.1 for a, b in zip(marks, marks[1:]))


def test_stop_penalty_trades_pennies_for_fewer_stops(plan, mock_route, make_station):
    # Greedy tops up at mile 10 to save 1 cent/gal; a $5 penalty skips that stop.
    make_station(mile=10, price=3.00)
    make_station(mile=480, price=3.01)
    mock_route(LON_START, lon_at_mile(900, LON_START))
    finish = pt(ROUTE_LAT, lon_at_mile(900, LON_START))

    pure = plan(START, finish, stop_penalty_usd=0).json()
    practical = plan(START, finish, stop_penalty_usd=5).json()

    assert len(pure["fuel_stops"]) == 2
    assert len(practical["fuel_stops"]) == 1
    assert Decimal(practical["summary"]["total_fuel_cost"]) - Decimal(pure["summary"]["total_fuel_cost"]) < Decimal("0.10")
    assert practical["summary"]["stop_penalty_usd"] == 5


@pytest.mark.parametrize("bad", [-1, 1001, "abc", True])
def test_invalid_stop_penalty_returns_400(plan, mocked_http, bad):
    resp = plan(START, FINISH, stop_penalty_usd=bad)
    assert resp.status_code == 400
    assert len(mocked_http.calls) == 0


# --------------------------------------------------------------------------- #
# Routing call contract
# --------------------------------------------------------------------------- #
def test_sends_one_osrm_request_with_lon_lat_order(plan, mock_route, mocked_http):
    mock_route(LON_START, -99.0)
    plan(pt(ROUTE_LAT, LON_START), pt(38.5, -99.0))

    calls = routing_calls(mocked_http)
    assert len(calls) == 1
    raw = calls[0].request.url
    # OSRM wants lon,lat;lon,lat in the path. Swapping these is the classic bug.
    assert raw.split("?")[0].endswith("/route/v1/driving/-104.000000,39.000000;-99.000000,38.500000")
    qs = parse_qs(urlparse(raw).query)
    assert qs["overview"] == ["full"] and qs["geometries"] == ["geojson"]
    assert "fuel-route-planner" in calls[0].request.headers["User-Agent"]


def test_start_equals_finish_makes_no_routing_call(plan, mocked_http):
    resp = plan(START, pt(ROUTE_LAT + 0.0001, LON_START))  # about 11 metres apart
    assert resp.status_code == 200
    body = resp.json()
    assert body["route"]["distance_miles"] == pytest.approx(0, abs=0.1)
    assert body["fuel_stops"] == []
    assert body["meta"]["routing_api_calls"] == 0
    assert len(mocked_http.calls) == 0


# --------------------------------------------------------------------------- #
# Station corridor matching
# --------------------------------------------------------------------------- #
def test_off_corridor_station_is_never_chosen(plan, mock_route, cross_state_stations):
    mock_route(LON_START, LON_END)
    body = plan(START, FINISH).json()
    chosen = {s["station_id"] for s in body["fuel_stops"]}
    assert cross_state_stations["OFF"].opis_id not in chosen


def test_station_within_corridor_reports_offset(plan, mock_route, make_station):
    near = make_station(mile=300, price=2.00, north_offset_miles=8)
    make_station(mile=600, price=3.00)
    mock_route(LON_START, LON_END)

    body = plan(START, FINISH).json()
    stop = next(s for s in body["fuel_stops"] if s["station_id"] == near.opis_id)
    assert stop["offset_miles"] == pytest.approx(8, abs=0.5)
    assert stop["route_mile"] == pytest.approx(300, abs=1)


def test_out_and_back_route_lists_station_once(plan, mocked_http, make_station):
    # East to mile 400, back west to mile 100 (700 mi). The station at mile 250
    # is passed twice (route miles 250 and 550); it must appear only once.
    station = make_station(mile=250, price=3.00)
    east = straight_route(LON_START, lon_at_mile(400, LON_START), points=300)
    back = straight_route(lon_at_mile(400, LON_START), lon_at_mile(100, LON_START), points=300)[1:]
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, json=osrm_route_body(east + back))

    body = plan(START, pt(ROUTE_LAT, lon_at_mile(100, LON_START))).json()
    ids = [s["station_id"] for s in body["fuel_stops"]]
    assert ids.count(station.opis_id) == 1
    assert body["fuel_stops"][0]["route_mile"] == pytest.approx(250, abs=2)


def test_route_that_crosses_canada_between_us_endpoints_is_allowed(plan, mocked_http):
    # Detroit -> Buffalo: the fastest road route runs through Ontario.
    coords = [[-83.05, 42.33], [-82.0, 42.30], [-80.5, 42.80], [-79.05, 43.10], [-78.88, 42.89]]
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, json=osrm_route_body(coords, distance_miles=255))
    resp = plan(pt(42.33, -83.05), pt(42.89, -78.88))
    assert resp.status_code == 200, resp.content
    assert resp.json()["fuel_stops"] == []


# --------------------------------------------------------------------------- #
# Infeasible and out-of-area
# --------------------------------------------------------------------------- #
def test_infeasible_route_returns_422_with_gap_details(plan, mock_route, make_station):
    make_station(mile=450, price=3.00)
    make_station(mile=960, price=3.00)  # 510 mi gap
    mock_route(LON_START, LON_END)

    resp = plan(START, FINISH)

    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "INFEASIBLE_ROUTE"
    assert err["details"]["from_mile"] == pytest.approx(450, abs=2)
    assert err["details"]["to_mile"] == pytest.approx(960, abs=2)
    assert err["details"]["gap_miles"] == pytest.approx(510, abs=3)
    assert err["details"]["near"]["from"]["lat"] == pytest.approx(ROUTE_LAT, abs=0.01)


def test_long_trip_with_no_stations_is_infeasible(plan, mock_route, db):
    mock_route(LON_START, LON_END)
    resp = plan(START, FINISH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "INFEASIBLE_ROUTE"


@pytest.mark.parametrize(
    "label,point",
    [
        ("Toronto, Canada", pt(43.65, -79.38)),
        ("Mexico City", pt(19.43, -99.13)),
        ("Atlantic Ocean", pt(30.0, -60.0)),
        ("Honolulu, HI", pt(21.31, -157.86)),
        ("Anchorage, AK", pt(61.22, -149.90)),
    ],
)
def test_endpoint_outside_service_area_is_rejected_before_routing(plan, mocked_http, label, point):
    resp = plan(point, FINISH)
    assert resp.status_code == 422, label
    assert resp.json()["error"]["code"] == "LOCATION_OUTSIDE_SERVICE_AREA"
    assert len(mocked_http.calls) == 0


@pytest.mark.parametrize("label,point", [("Detroit, MI", pt(42.3314, -83.0458)), ("El Paso, TX", pt(31.7619, -106.4850))])
def test_border_cities_are_inside_service_area(plan, mock_route, label, point):
    # finish 1 degree west, so both ends are clearly on the US side of the border
    mock_route(point["lon"], point["lon"] - 1, lat=point["lat"])
    assert plan(point, pt(point["lat"], point["lon"] - 1)).status_code == 200, label


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "payload",
    [
        {"start": START},  # missing finish
        {"finish": FINISH},  # missing start
        {"start": "", "finish": FINISH},  # empty string
        {"start": "   ", "finish": FINISH},  # whitespace only
        {"start": pt(200, -100), "finish": FINISH},  # lat out of range
        {"start": pt(39, -300), "finish": FINISH},  # lon out of range
        {"start": {"lat": "abc", "lon": -100}, "finish": FINISH},
        {"start": {"lat": 39}, "finish": FINISH},  # missing lon
        {"start": {"lat": None, "lon": None}, "finish": FINISH},
        {"start": {"lat": True, "lon": -100}, "finish": FINISH},  # bool is not a number
        {"start": 12345, "finish": FINISH},  # wrong type
        {"start": ["Denver", "CO"], "finish": FINISH},  # wrong type
        {"start": "x" * 201, "finish": FINISH},  # too long
        {"start": {"lat": float("nan"), "lon": -100}, "finish": FINISH},
    ],
)
def test_invalid_payload_returns_400(client, mocked_http, payload):
    resp = client.post(API_URL, data=json.dumps(payload, allow_nan=True), content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    assert len(mocked_http.calls) == 0


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", "null", ""])
def test_malformed_or_non_object_json_returns_400(client, mocked_http, body):
    resp = client.post(API_URL, data=body, content_type="application/json")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_unsupported_methods_return_405(client, method):
    assert getattr(client, method)(API_URL).status_code == 405


def test_get_with_query_params_is_supported(client, mock_route):
    mock_route(LON_START, -99.0)
    resp = client.get(API_URL, {"start": f"{ROUTE_LAT},{LON_START}", "finish": f"{ROUTE_LAT},-99.0"})
    assert resp.status_code == 200


def test_get_without_params_returns_400(client):
    resp = client.get(API_URL)
    assert resp.status_code == 400
    assert set(resp.json()["error"]["details"]["fields"]) == {"start", "finish"}


# --------------------------------------------------------------------------- #
# Location resolution (all local, zero external calls)
# --------------------------------------------------------------------------- #
@pytest.fixture
def denver_indy_station(make_station):
    """Denver -> Indianapolis is ~1000 mi, so the trip needs stations on the way."""
    return [make_station(lat=39.75, lon=-97.5, price=3.00), make_station(lat=39.75, lon=-91.0, price=3.10)]


def test_city_state_resolves_locally(plan, mock_route, mocked_http, denver_indy_station):
    mock_route(-104.99, -86.16, lat=39.75)
    resp = plan("Denver, CO", "Indianapolis, IN")

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["start"]["lat"] == pytest.approx(39.7392, abs=1e-3)
    assert body["start"]["resolved_by"] == "gazetteer"
    assert body["start"]["label"] == "Denver, CO"
    assert body["meta"]["geocoding_api_calls"] == 0
    assert len(mocked_http.calls) == 1  # only the routing call


@pytest.mark.parametrize(
    "start,finish",
    [
        ("  denver ,  co ", "INDIANAPOLIS, in"),
        ("Denver, Colorado", "Indianapolis, Indiana"),
        ("Denver CO", "Indianapolis IN"),
        ("Denver, CO, USA", "Indianapolis, IN, US"),
    ],
)
def test_city_input_variants(plan, mock_route, denver_indy_station, start, finish):
    mock_route(-104.99, -86.16, lat=39.75)
    assert plan(start, finish).status_code == 200


def test_lat_lon_string_is_parsed_as_coordinates(plan, mock_route, mocked_http):
    mock_route(LON_START, -99.0)
    resp = plan("39.0, -104.0", "39.0,-99.0")
    assert resp.status_code == 200
    assert resp.json()["start"]["resolved_by"] == "coordinates"


def test_ambiguous_city_without_state_returns_400(plan, mocked_http):
    resp = plan("Springfield", FINISH)
    assert resp.status_code == 400
    err = resp.json()["error"]
    assert err["code"] == "LOCATION_AMBIGUOUS"
    assert {c["state"] for c in err["details"]["candidates"]} == {"IL", "MO"}
    assert len(mocked_http.calls) == 0


def test_unique_city_without_state_resolves(plan, mock_route, make_station):
    make_station(lat=38.88, lon=-93.0, price=3.00)  # route is ~717 mi, needs one stop
    mock_route(-99.33, LON_END, lat=38.88)
    assert plan("Hays", FINISH).status_code == 200


@pytest.mark.parametrize(
    "query", ["1600 Pennsylvania Ave NW, Washington, DC", "Qwertyuiop Nowhereville", "Atlantis, FL", "Denver, ZZ"]
)
def test_unknown_place_returns_422_without_any_call(plan, mocked_http, query):
    resp = plan(query, FINISH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "LOCATION_NOT_FOUND"
    assert len(mocked_http.calls) == 0


# --------------------------------------------------------------------------- #
# Routing failures: mapping, fallback and call counts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mock_kwargs,status,code",
    [
        ({"status": 500, "json": {"error": "boom"}}, 502, "ROUTING_PROVIDER_ERROR"),
        ({"status": 503, "body": "<html>down</html>"}, 502, "ROUTING_PROVIDER_ERROR"),
        ({"status": 200, "json": {"unexpected": True}}, 502, "ROUTING_PROVIDER_ERROR"),
        ({"status": 200, "json": {"code": "Ok", "routes": []}}, 502, "ROUTING_PROVIDER_ERROR"),
        ({"status": 200, "body": "not json"}, 502, "ROUTING_PROVIDER_ERROR"),
        ({"status": 429, "json": {"message": "rate"}, "headers": {"Retry-After": "30"}}, 503, "ROUTING_RATE_LIMITED"),
        ({"status": 400, "json": osrm_error_body("NoRoute")}, 422, "NO_ROUTE"),
        ({"status": 400, "json": osrm_error_body("NoSegment")}, 422, "NO_ROUTE"),
        ({"status": 400, "json": osrm_error_body("TooBig")}, 422, "ROUTE_TOO_LONG"),
        ({"body": requests.exceptions.ReadTimeout()}, 504, "ROUTING_TIMEOUT"),
        ({"body": requests.exceptions.ConnectionError()}, 502, "ROUTING_PROVIDER_ERROR"),
    ],
)
def test_osrm_failures_are_mapped_when_no_fallback(settings, plan, mocked_http, mock_kwargs, status, code):
    settings.ROUTING_PROVIDERS = ["osrm"]
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, **mock_kwargs)

    resp = plan(START, FINISH)

    assert resp.status_code == status
    err = resp.json()["error"]
    assert err["code"] == code
    assert len(routing_calls(mocked_http)) == 1, "no automatic retries"
    assert "osrm.test" not in resp.content.decode(), "provider URL leaked in error body"
    if code == "ROUTING_RATE_LIMITED":
        assert resp["Retry-After"] == "30"


def test_falls_back_to_valhalla_when_osrm_is_down(plan, mocked_http, make_station):
    make_station(mile=400, price=3.00)
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, status=502, body="bad gateway")
    mocked_http.add(responses.POST, VALHALLA_ROUTE_URL, json=valhalla_route_body(straight_route(LON_START, -92.0)))

    resp = plan(START, pt(ROUTE_LAT, -92.0))  # about 645 mi -> one stop

    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["route"]["provider"] == "valhalla"
    assert body["meta"]["routing_api_calls"] == 2
    assert len(body["fuel_stops"]) == 1
    sent = json.loads(valhalla_calls(mocked_http)[0].request.body)
    assert sent["locations"] == [{"lat": ROUTE_LAT, "lon": LON_START}, {"lat": ROUTE_LAT, "lon": -92.0}]


def test_no_route_from_osrm_is_final_and_skips_fallback(plan, mocked_http):
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, status=400, json=osrm_error_body("NoRoute"))
    resp = plan(START, FINISH)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "NO_ROUTE"
    assert len(valhalla_calls(mocked_http)) == 0


def test_both_providers_failing_reports_first_error_with_attempts(plan, mocked_http):
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, body=requests.exceptions.ReadTimeout())
    mocked_http.add(responses.POST, VALHALLA_ROUTE_URL, status=500, json={})

    resp = plan(START, FINISH)

    assert resp.status_code == 504
    err = resp.json()["error"]
    assert err["code"] == "ROUTING_TIMEOUT"
    assert [a["provider"] for a in err["details"]["attempts"]] == ["osrm", "valhalla"]
    assert len(routing_calls(mocked_http)) == 2, "never more than one call per provider"


@pytest.mark.parametrize(
    "error_code,status,code",
    [(442, 422, "NO_ROUTE"), (171, 422, "NO_ROUTE"), (154, 422, "ROUTE_TOO_LONG")],
)
def test_valhalla_error_codes_are_mapped(settings, plan, mocked_http, error_code, status, code):
    settings.ROUTING_PROVIDERS = ["valhalla"]
    mocked_http.add(responses.POST, VALHALLA_ROUTE_URL, status=400, json={"error_code": error_code, "error": "x"})
    resp = plan(START, FINISH)
    assert resp.status_code == status
    assert resp.json()["error"]["code"] == code


def test_failed_routing_is_not_cached(plan, mocked_http, mock_route):
    mocked_http.add(responses.GET, OSRM_ROUTE_URL, status=500, json={})
    mocked_http.add(responses.POST, VALHALLA_ROUTE_URL, status=500, json={})
    assert plan(START, pt(ROUTE_LAT, -99.0)).status_code == 502
    mocked_http.reset()
    mock_route(LON_START, -99.0)
    assert plan(START, pt(ROUTE_LAT, -99.0)).status_code == 200


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #
def test_repeat_request_is_served_from_cache(plan, mock_route, mocked_http, cross_state_stations):
    mock_route(LON_START, LON_END)
    first = plan(START, FINISH).json()
    second = plan(START, FINISH).json()

    assert len(routing_calls(mocked_http)) == 1
    assert first["meta"]["cache_hit"] is False
    assert second["meta"]["cache_hit"] is True
    assert second["meta"]["routing_api_calls"] == 0
    assert second["fuel_stops"] == first["fuel_stops"]
    assert second["summary"] == first["summary"]
    assert second["map_url"] == first["map_url"]


def test_tiny_coordinate_jitter_hits_cache(plan, mock_route, mocked_http):
    mock_route(LON_START, -99.0)
    plan(START, pt(ROUTE_LAT, -99.0))
    plan(pt(ROUTE_LAT + 1e-6, LON_START - 1e-6), pt(ROUTE_LAT, -99.0 + 1e-6))
    assert len(routing_calls(mocked_http)) == 1


def test_reversed_trip_is_a_different_cache_key(plan, mock_route, mocked_http):
    mock_route(LON_START, -99.0)
    mock_route(-99.0, LON_START)
    plan(START, pt(ROUTE_LAT, -99.0))
    plan(pt(ROUTE_LAT, -99.0), START)
    assert len(routing_calls(mocked_http)) == 2


def test_price_change_is_reflected_on_cached_route(plan, mock_route, mocked_http, cross_state_stations):
    """The route is cached, the fuel plan is not: new prices apply immediately."""
    from trips.services.station_index import StationIndex

    mock_route(LON_START, LON_END)
    first = plan(START, FINISH).json()
    s4 = cross_state_stations["S4"]
    s4.retail_price = Decimal("4.50")
    s4.save()
    StationIndex.reset()
    second = plan(START, FINISH).json()

    assert len(routing_calls(mocked_http)) == 1
    assert second["meta"]["cache_hit"] is True
    assert second["summary"]["total_fuel_cost"] != first["summary"]["total_fuel_cost"]


# --------------------------------------------------------------------------- #
# Map endpoint
# --------------------------------------------------------------------------- #
def test_map_page_renders_route_and_stops_without_new_calls(client, plan, mock_route, mocked_http, cross_state_stations):
    mock_route(LON_START, LON_END)
    body = plan(START, FINISH).json()

    resp = client.get(body["map_url"])

    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/html")
    html = resp.content.decode()
    assert "leaflet" in html.lower()
    assert cross_state_stations["S2"].name in html
    assert len(routing_calls(mocked_http)) == 1


def test_map_page_escapes_station_names(client, plan, mock_route, make_station):
    make_station(mile=420, price=2.00, name="<script>alert(1)</script>")
    make_station(mile=800, price=3.00)
    mock_route(LON_START, LON_END)
    body = plan(START, FINISH).json()
    html = client.get(body["map_url"]).content.decode()
    assert "<script>alert(1)</script>" not in html


def test_unknown_map_id_returns_404(client):
    assert client.get(f"{API_URL}00000000-0000-0000-0000-000000000000/map/").status_code == 404


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def test_warm_request_is_fast_with_realistic_station_count(plan, mock_route, db):
    from trips.models import FuelStation

    FuelStation.objects.bulk_create(
        [
            FuelStation(
                opis_id=500_000 + i,
                name=f"BULK {i}",
                address="",
                city="X",
                state="KS",
                retail_price=Decimal("3.000") + Decimal(i % 97) / 100,
                lat=25 + (i % 230) / 10,
                lon=-124 + (i // 230) * 1.6,
                geocode_source="test",
            )
            for i in range(7000)
        ]
    )
    mock_route(LON_START, LON_END)
    plan(START, FINISH)  # warms the station index
    mock_route(LON_START + 0.5, LON_END, coords=straight_route(LON_START + 0.5, LON_END, points=20000))

    t0 = time.perf_counter()
    resp = plan(pt(ROUTE_LAT, LON_START + 0.5), FINISH)
    elapsed = time.perf_counter() - t0

    assert resp.status_code == 200, resp.content
    assert elapsed < 0.3, f"warm request took {elapsed:.3f}s"


# --------------------------------------------------------------------------- #
# Live smoke tests (opt-in: pytest -m live; needs network to the public servers)
# --------------------------------------------------------------------------- #
LIVE_TRIPS = [
    ("Chicago, IL", "Dallas, TX", 200),
    ("New York, NY", "Los Angeles, CA", 200),  # reachable from the last Arizona stations
    ("Seattle, WA", "Los Angeles, CA", 422),  # I-5: no stations in the file from Oregon to LA
    ("New York, NY", "San Diego, CA", 200),
    ("Seattle, WA", "Miami, FL", 200),
]


@pytest.mark.live
@pytest.mark.parametrize("start,finish,status", LIVE_TRIPS)
def test_live_trip(settings, client, start, finish, status):
    from django.core.management import call_command

    from trips.models import FuelStation

    settings.ROUTING_PROVIDERS = ["osrm", "valhalla"]
    settings.OSRM_BASE_URL = os.environ.get("OSRM_BASE_URL", "https://router.project-osrm.org")
    settings.VALHALLA_BASE_URL = os.environ.get("VALHALLA_BASE_URL", "https://valhalla1.openstreetmap.de")
    settings.GAZETTEER_PATH = settings.BASE_DIR / "data" / "us_places_gazetteer.csv"
    if not FuelStation.objects.exists():
        call_command("import_stations", str(settings.BASE_DIR / "data" / "fuel-prices-for-be-assessment.csv"))

    t0 = time.perf_counter()
    resp = client.post(API_URL, data=json.dumps({"start": start, "finish": finish}), content_type="application/json")
    assert resp.status_code == status, resp.content
    if status == 200:
        body = resp.json()
        assert body["meta"]["routing_api_calls"] == 1
        marks = [0.0] + [s["route_mile"] for s in body["fuel_stops"]] + [body["route"]["distance_miles"]]
        assert all(b - a <= 500.5 for a, b in zip(marks, marks[1:]))
    assert time.perf_counter() - t0 < 10
