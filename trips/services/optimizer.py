"""Cost-optimal refuelling along a fixed route ("gas station problem").

Greedy rule, provably optimal for a single path with a fixed tank and linear
prices (Khuller, Malekian, Mestre, "To Fill or Not to Fill", 2007):

  at each stop,
    - if a cheaper station is within range: buy just enough to reach it
    - elif the destination is within range: buy just enough to arrive empty
    - else: fill the tank and drive to the cheapest station in range
            (ties -> the farther one, for fewer stops and deterministic output)

The starting tank is modelled as a virtual station at mile 0 with price -1, so
it is always "the cheapest" and is used first without being charged.

Pure cost-optimal plans can include silly top-ups (buy 0.8 gal to save 3 cents).
With `stop_penalty` > 0 each stop is charged that many dollars *for choosing
only* (never added to the reported fuel cost), and an exact dynamic programme
over tank levels (0.01 gal steps) replaces the greedy rule. With stop_penalty=0
the greedy result is returned, so the two modes agree on pure fuel cost.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

EPS = 1e-6
GALLONS_Q = Decimal("0.001")
CENTS = Decimal("0.01")
_VIRTUAL_START_PRICE = Decimal("-1")


@dataclass(frozen=True)
class RouteStop:
    station_id: int
    route_mile: float
    price: Decimal


@dataclass(frozen=True)
class PlannedStop:
    station_id: int
    route_mile: float
    price: Decimal
    gallons: Decimal
    cost: Decimal


@dataclass(frozen=True)
class FuelPlan:
    stops: list = field(default_factory=list)
    total_cost: Decimal = Decimal("0.00")
    gallons_purchased: Decimal = Decimal("0.000")
    gallons_consumed: Decimal = Decimal("0.000")


class InfeasibleRouteError(Exception):
    """A stretch of the route is longer than the vehicle range with no fuel."""

    def __init__(self, from_mile: float, to_mile: float):
        self.from_mile = float(from_mile)
        self.to_mile = float(to_mile)
        super().__init__(f"No fuel between mile {self.from_mile:.1f} and mile {self.to_mile:.1f}")


def _q(value: float, quantum: Decimal) -> Decimal:
    return Decimal(repr(value)).quantize(quantum, rounding=ROUND_HALF_UP)


def plan_fuel_stops(
    stations: list[RouteStop],
    route_miles: float,
    *,
    max_range_miles: float = 500.0,
    mpg: float = 10.0,
    start_full: bool = True,
    stop_penalty: float = 0.0,
) -> FuelPlan:
    greedy = _plan_greedy(stations, route_miles, max_range_miles=max_range_miles, mpg=mpg, start_full=start_full)
    if stop_penalty <= 0 or len(greedy.stops) <= 1:
        return greedy
    return _plan_dp(stations, route_miles, max_range_miles, mpg, start_full, float(stop_penalty))


def _finish(purchases, route_miles, mpg) -> FuelPlan:
    stops = []
    for sid, mile, price, gallons in purchases:
        g = gallons if isinstance(gallons, Decimal) else _q(gallons, GALLONS_Q)
        if g <= 0:
            continue
        stops.append(PlannedStop(sid, mile, price, g, (price * g).quantize(CENTS, rounding=ROUND_HALF_UP)))
    return FuelPlan(
        stops=stops,
        total_cost=sum((s.cost for s in stops), Decimal("0.00")),
        gallons_purchased=sum((s.gallons for s in stops), Decimal("0.000")),
        gallons_consumed=_q(route_miles / mpg, GALLONS_Q),
    )


def _plan_greedy(stations, route_miles, *, max_range_miles, mpg, start_full) -> FuelPlan:
    route_miles = max(0.0, float(route_miles))
    tank = max_range_miles / mpg

    usable = sorted(
        (s for s in stations if -EPS <= s.route_mile <= route_miles + EPS),
        key=lambda s: (s.route_mile, s.price, s.station_id),
    )
    # nodes: (mile, price, station_id or None for the virtual start)
    nodes: list[tuple[float, Decimal, int | None]] = []
    if start_full:
        nodes.append((0.0, _VIRTUAL_START_PRICE, None))
    elif not usable or usable[0].route_mile > EPS:
        if route_miles <= EPS:
            return FuelPlan()
        raise InfeasibleRouteError(0.0, usable[0].route_mile if usable else route_miles)
    nodes.extend((max(0.0, s.route_mile), s.price, s.station_id) for s in usable)

    n = len(nodes)
    # next strictly cheaper node for each node (monotonic stack, O(n))
    next_cheaper = [None] * n
    stack: list[int] = []
    for i in range(n - 1, -1, -1):
        while stack and nodes[stack[-1]][1] >= nodes[i][1]:
            stack.pop()
        next_cheaper[i] = stack[-1] if stack else None
        stack.append(i)

    fuel = tank if start_full else 0.0
    purchases: list[tuple[int, float, Decimal, float]] = []
    i = 0
    while True:
        mile, price, sid = nodes[i]
        reach_limit = mile + max_range_miles + EPS
        j = next_cheaper[i]
        if j is not None and nodes[j][0] <= reach_limit:
            target, need = j, (nodes[j][0] - mile) / mpg
        elif route_miles <= reach_limit:
            target, need = None, (route_miles - mile) / mpg
        else:
            # fill up, then go to the cheapest node within range (farthest on ties)
            best = None
            k = i + 1
            while k < n and nodes[k][0] <= reach_limit:
                if best is None or nodes[k][1] <= nodes[best][1]:
                    best = k
                k += 1
            if best is None:
                raise InfeasibleRouteError(mile, nodes[i + 1][0] if i + 1 < n else route_miles)
            target, need = best, tank

        buy = max(0.0, min(need, tank) - fuel)
        if sid is not None and buy > 1e-9:
            purchases.append((sid, mile, price, buy))
        fuel += buy
        if target is None:
            break
        fuel -= (nodes[target][0] - mile) / mpg
        fuel = max(fuel, 0.0)  # float dust
        i = target

    return _finish(purchases, route_miles, mpg)


def _plan_dp(stations, route_miles, max_range_miles, mpg, start_full, penalty) -> FuelPlan:
    """Exact minimum of (fuel cost + penalty x stops) over discretised tank levels.

    Only called once the greedy pass has shown the trip is feasible.
    dp[g] = cheapest way to be at the current station holding g units of fuel.
    Buying from level f up to g costs penalty + price*(g - f); the best f for
    every g is a prefix minimum, so each station is O(levels) with numpy.
    """
    route_miles = max(0.0, float(route_miles))
    usable = sorted(
        (s for s in stations if -EPS <= s.route_mile <= route_miles + EPS),
        key=lambda s: (s.route_mile, s.price, s.station_id),
    )
    unit = 0.01 if len(usable) <= 1500 else 0.1  # gallons per level
    levels = int(round(max_range_miles / mpg / unit))
    idx = np.arange(levels + 1)
    inf = np.inf

    def units(miles):
        return int(round(miles / mpg / unit))

    dp = np.full(levels + 1, inf)
    start_level = levels if start_full else 0
    first_leg = units(usable[0].route_mile) if usable else units(route_miles)
    if first_leg > start_level:  # a negative index would silently wrap around
        raise InfeasibleRouteError(0.0, usable[0].route_mile if usable else route_miles)
    dp[start_level - first_leg] = 0.0
    choices = []
    for i, st in enumerate(usable):
        price_u = float(st.price) * unit
        v = dp - price_u * idx
        pm = np.minimum.accumulate(v)
        arg = np.maximum.accumulate(np.where(v <= pm, idx, 0))
        # best source strictly below g: shift prefix by one
        best_prev = np.concatenate([[inf], pm[:-1]])
        src_prev = np.concatenate([[0], arg[:-1]])
        buy_cost = penalty + price_u * idx + best_prev
        after = np.minimum(dp, buy_cost)
        choice = np.where(buy_cost < dp, src_prev, idx)
        choices.append(choice)
        nxt = usable[i + 1].route_mile if i + 1 < len(usable) else route_miles
        leg = units(nxt - max(0.0, st.route_mile))
        dp = np.full(levels + 1, inf)
        if leg <= levels:
            dp[: levels + 1 - leg] = after[leg:]
        dp_final_leg = leg
        last_after = after
    if not usable:
        return FuelPlan(gallons_consumed=_q(route_miles / mpg, GALLONS_Q))

    # arrival at destination: any non-negative leftover is fine; take the cheapest
    g = int(np.argmin(dp)) + dp_final_leg  # level held when leaving the last station
    if not np.isfinite(last_after[g]):  # should not happen after a feasible greedy pass
        raise InfeasibleRouteError(usable[-1].route_mile, route_miles)
    purchases = []
    for i in range(len(usable) - 1, -1, -1):
        f = int(choices[i][g])
        if f < g:
            st = usable[i]
            purchases.append((st.station_id, max(0.0, st.route_mile), st.price,
                              (Decimal(g - f) * Decimal(str(unit))).quantize(GALLONS_Q)))
        prev_mile = usable[i - 1].route_mile if i > 0 else 0.0
        g = f + units(max(0.0, usable[i].route_mile) - max(0.0, prev_mile))
    purchases.reverse()
    return _finish(purchases, route_miles, mpg)
