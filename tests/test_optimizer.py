"""Tests for the fuel stop optimiser (pure logic, no DB, no HTTP).

The hand-worked cases pin down exact behaviour. The DP cross-check checks
optimality on random instances against an independent brute-force solver.
"""
import random
import time
from decimal import Decimal

import pytest

from trips.services.optimizer import InfeasibleRouteError, RouteStop, plan_fuel_stops

TANK = 50  # gallons (500 mi / 10 mpg)


def s(station_id, mile, price):
    return RouteStop(station_id=station_id, route_mile=float(mile), price=Decimal(str(price)))


def ids(plan):
    return [p.station_id for p in plan.stops]


# --------------------------------------------------------------------------- #
# Hand-worked cases
# --------------------------------------------------------------------------- #
def test_short_trip_needs_no_stops():
    plan = plan_fuel_stops([s(1, 100, 3.0)], route_miles=400)
    assert plan.stops == []
    assert plan.total_cost == Decimal("0.00")
    assert plan.gallons_consumed == Decimal("40.000")


def test_zero_length_trip():
    plan = plan_fuel_stops([], route_miles=0)
    assert plan.stops == [] and plan.total_cost == Decimal("0.00")


def test_exactly_500_miles_is_feasible_without_stops():
    plan = plan_fuel_stops([], route_miles=500.0)
    assert plan.stops == []


def test_500_miles_plus_epsilon_float_noise_is_still_feasible():
    plan = plan_fuel_stops([], route_miles=500.0000000001)
    assert plan.stops == []


def test_cheapest_in_range_then_top_up_to_destination():
    # 0 -> 300 is cheapest in first 500 mi; destination at 800 is exactly 500 from 300.
    stations = [s(1, 100, 4.00), s(2, 300, 3.00), s(3, 450, 5.00)]
    plan = plan_fuel_stops(stations, route_miles=800)
    assert ids(plan) == [2]
    assert plan.stops[0].gallons == Decimal("30.000")
    assert plan.total_cost == Decimal("90.00")


def test_buys_just_enough_to_reach_a_cheaper_station():
    # At mile 400 ($3.00) a cheaper station sits at 600 ($2.50): buy only 10 gal at 400.
    stations = [s(1, 400, 3.00), s(2, 600, 2.50), s(3, 900, 4.00)]
    plan = plan_fuel_stops(stations, route_miles=1000)
    assert ids(plan) == [1, 2]
    assert [p.gallons for p in plan.stops] == [Decimal("10.000"), Decimal("40.000")]
    assert plan.total_cost == Decimal("130.00")


def test_fills_up_when_nothing_cheaper_is_ahead():
    # Cheap at 450, pricier stations after it -> fill the tank at 450.
    stations = [s(1, 450, 2.00), s(2, 700, 4.00), s(3, 940, 4.50)]
    plan = plan_fuel_stops(stations, route_miles=1300)
    assert ids(plan)[0] == 1
    assert plan.stops[0].gallons == Decimal("45.000")  # arrives with 5, fills to 50
    # remaining: reach 950 on the fill, must buy again at 700 or 940 for the last 350 mi
    assert ids(plan)[1] == 2


def test_cheaper_station_just_out_of_range_is_not_used_as_first_hop():
    # $1.00 station at mile 501 is beyond the start tank; must stop at 300 first.
    stations = [s(1, 300, 4.00), s(2, 501, 1.00)]
    plan = plan_fuel_stops(stations, route_miles=900)
    assert ids(plan) == [1, 2]
    # at 300 buy just enough to reach 501 (arrive with 20, need 20.1)
    assert plan.stops[0].gallons == Decimal("0.100")


def test_equal_prices_prefer_the_farther_station():
    stations = [s(1, 200, 3.00), s(2, 400, 3.00)]
    plan = plan_fuel_stops(stations, route_miles=700)
    assert ids(plan) == [2]


def test_output_is_deterministic_regardless_of_input_order():
    stations = [s(1, 400, 3.00), s(2, 600, 2.50), s(3, 900, 4.00)]
    a = plan_fuel_stops(stations, route_miles=1000)
    b = plan_fuel_stops(list(reversed(stations)), route_miles=1000)
    assert a == b


def test_stations_past_destination_are_ignored():
    plan = plan_fuel_stops([s(1, 900, 0.01)], route_miles=450)
    assert plan.stops == []


def test_station_at_mile_zero_with_full_tank_buys_nothing():
    plan = plan_fuel_stops([s(1, 0, 1.00), s(2, 480, 3.00)], route_miles=900)
    # mile-0 station is cheap but the tank is already full; it can't be used.
    assert all(p.gallons > 0 for p in plan.stops)
    assert 1 not in ids(plan)


def test_start_empty_mode_requires_station_at_start():
    with pytest.raises(InfeasibleRouteError):
        plan_fuel_stops([s(1, 50, 3.0)], route_miles=300, start_full=False)
    plan = plan_fuel_stops([s(1, 0, 3.0)], route_miles=300, start_full=False)
    assert plan.total_cost == Decimal("90.00")


# --------------------------------------------------------------------------- #
# Infeasible routes
# --------------------------------------------------------------------------- #
def test_gap_between_stations_over_500_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as exc:
        plan_fuel_stops([s(1, 450, 3.0), s(2, 1000, 3.0)], route_miles=1200)
    assert exc.value.from_mile == pytest.approx(450)
    assert exc.value.to_mile == pytest.approx(1000)


def test_no_stations_on_a_long_route_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as exc:
        plan_fuel_stops([], route_miles=600)
    assert exc.value.from_mile == pytest.approx(0)
    assert exc.value.to_mile == pytest.approx(600)


def test_last_station_too_far_from_destination_is_infeasible():
    with pytest.raises(InfeasibleRouteError) as exc:
        plan_fuel_stops([s(1, 300, 3.0)], route_miles=900)
    assert exc.value.from_mile == pytest.approx(300)
    assert exc.value.to_mile == pytest.approx(900)


# --------------------------------------------------------------------------- #
# Money and invariants
# --------------------------------------------------------------------------- #
def test_total_equals_sum_of_rounded_stop_costs():
    stations = [s(1, 333.33, 3.00733333), s(2, 777.77, 2.91566666), s(3, 1111.11, 3.43233333)]
    plan = plan_fuel_stops(stations, route_miles=1500)
    assert plan.total_cost == sum((p.cost for p in plan.stops), Decimal("0"))
    for p in plan.stops:
        assert p.cost == p.cost.quantize(Decimal("0.01"))


def simulate(plan, route_miles, start_gallons=TANK):
    """Replay the plan mile by mile; return min and max tank level seen."""
    fuel, pos, lo, hi = float(start_gallons), 0.0, float(start_gallons), float(start_gallons)
    for stop in plan.stops:
        fuel -= (stop.route_mile - pos) / 10
        lo = min(lo, fuel)
        fuel += float(stop.gallons)
        hi = max(hi, fuel)
        pos = stop.route_mile
    fuel -= (route_miles - pos) / 10
    return min(lo, fuel), hi


def random_instance(rng, n=None):
    route = rng.choice([600, 900, 1500, 2400, 2900])
    n = n or rng.randint(5, 40)
    miles = sorted(rng.sample(range(10, route, 10), k=min(n, route // 10 - 1)))
    stations = [s(i, m, round(rng.uniform(2.7, 4.6), 3)) for i, m in enumerate(miles)]
    return stations, route


@pytest.mark.parametrize("seed", range(50))
def test_tank_never_below_zero_or_above_capacity(seed):
    rng = random.Random(seed)
    stations, route = random_instance(rng)
    try:
        plan = plan_fuel_stops(stations, route_miles=route)
    except InfeasibleRouteError:
        return
    lo, hi = simulate(plan, route)
    assert lo >= -1e-6
    assert hi <= TANK + 1e-6
    assert all(p.gallons > 0 for p in plan.stops)
    miles = [p.route_mile for p in plan.stops]
    assert miles == sorted(miles)


# --------------------------------------------------------------------------- #
# Optimality cross-check against brute-force DP
# --------------------------------------------------------------------------- #
def dp_min_cost(stations, route_miles, tank=TANK):
    """Exact min cost when all positions are multiples of 10 mi (1 gal units).

    State = (station index, gallons on arrival). Integer gallons are exact here
    because every leg is a whole number of gallons.
    """
    pts = sorted(stations, key=lambda x: x.route_mile)
    pts = [p for p in pts if p.route_mile <= route_miles]
    INF = float("inf")
    # arriving at first station from start with full tank
    best = {}
    first_leg = int(round(pts[0].route_mile / 10)) if pts else int(round(route_miles / 10))
    if first_leg > tank:
        return None
    if not pts:
        return 0.0
    best = {tank - first_leg: 0.0}
    for i, st in enumerate(pts):
        nxt = pts[i + 1].route_mile if i + 1 < len(pts) else route_miles
        leg = int(round((nxt - st.route_mile) / 10))
        new = {}
        for fuel, cost in best.items():
            for buy in range(0, tank - fuel + 1):
                have = fuel + buy
                if have < leg:
                    continue
                c = cost + buy * float(st.price)
                left = have - leg
                if c < new.get(left, INF):
                    new[left] = c
        if not new:
            return None
        best = new
    return min(best.values())


@pytest.mark.parametrize("seed", range(200))
def test_greedy_matches_brute_force_dp(seed):
    rng = random.Random(1000 + seed)
    stations, route = random_instance(rng, n=rng.randint(3, 12))
    expected = dp_min_cost(stations, route)
    if expected is None:
        with pytest.raises(InfeasibleRouteError):
            plan_fuel_stops(stations, route_miles=route)
        return
    plan = plan_fuel_stops(stations, route_miles=route)
    # cent-rounding per stop can drift a few cents from the float DP
    assert float(plan.total_cost) == pytest.approx(expected, abs=0.01 * (len(plan.stops) + 1))


# --------------------------------------------------------------------------- #
# Performance
# --------------------------------------------------------------------------- #
def test_handles_thousands_of_stations_quickly():
    rng = random.Random(7)
    stations = [s(i, rng.uniform(0, 3000), round(rng.uniform(2.7, 4.6), 3)) for i in range(5000)]
    t0 = time.perf_counter()
    plan_fuel_stops(stations, route_miles=3000)
    assert time.perf_counter() - t0 < 0.2


# --------------------------------------------------------------------------- #
# Stop-penalty mode (exact DP over tank levels)
# --------------------------------------------------------------------------- #
def objective(plan, penalty):
    return float(plan.total_cost) + penalty * len(plan.stops)


def test_zero_penalty_returns_the_greedy_plan():
    stations = [s(1, 400, 3.00), s(2, 600, 2.50), s(3, 900, 4.00)]
    assert plan_fuel_stops(stations, 1000, stop_penalty=0) == plan_fuel_stops(stations, 1000)


def test_penalty_removes_a_pointless_top_up():
    # Greedy tops up 1 gal at mile 10 to save 1 cent per gallon; a $5 stop penalty skips it.
    stations = [s(1, 10, 3.00), s(2, 480, 3.01)]
    greedy = plan_fuel_stops(stations, 900)
    assert ids(greedy) == [1, 2]
    practical = plan_fuel_stops(stations, 900, stop_penalty=5)
    assert ids(practical) == [2]
    assert practical.stops[0].gallons == Decimal("40.000")
    assert practical.total_cost == Decimal("120.40")


@pytest.mark.parametrize("seed", range(60))
@pytest.mark.parametrize("penalty", [2.0, 10.0])
def test_penalty_mode_is_feasible_and_never_worse_on_its_objective(seed, penalty):
    rng = random.Random(5000 + seed)
    stations, route = random_instance(rng, n=rng.randint(5, 30))
    try:
        greedy = plan_fuel_stops(stations, route_miles=route)
    except InfeasibleRouteError:
        with pytest.raises(InfeasibleRouteError):
            plan_fuel_stops(stations, route_miles=route, stop_penalty=penalty)
        return
    dp = plan_fuel_stops(stations, route_miles=route, stop_penalty=penalty)
    lo, hi = simulate(dp, route)
    assert lo >= -0.02 and hi <= TANK + 1e-6
    assert len(dp.stops) <= len(greedy.stops)
    assert float(dp.total_cost) >= float(greedy.total_cost) - 0.05
    assert objective(dp, penalty) <= objective(greedy, penalty) + 0.05


def test_penalty_mode_is_fast_on_a_realistic_corridor():
    rng = random.Random(11)
    stations = [s(i, rng.uniform(0, 3000), round(rng.uniform(2.7, 4.6), 3)) for i in range(400)]
    t0 = time.perf_counter()
    plan_fuel_stops(stations, route_miles=3000, stop_penalty=10)
    assert time.perf_counter() - t0 < 0.25


def test_dp_rejects_unreachable_first_station_instead_of_wrapping():
    from trips.services.optimizer import _plan_dp

    stations = [RouteStop(1, 600.0, Decimal("3.00")), RouteStop(2, 900.0, Decimal("3.10"))]
    with pytest.raises(InfeasibleRouteError):
        _plan_dp(stations, 1200.0, 500.0, 10.0, True, 5.0)
