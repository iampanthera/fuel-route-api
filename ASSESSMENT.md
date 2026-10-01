# Assessment: Fuel Route Planner API

## The brief

Build an API that:

1. Takes a **start** and **finish** location, both within the USA.
2. Returns a **map of the route**, along with the **optimal places to fuel up** along it. "Optimal" mostly means cost-effective, based on fuel prices.
3. Assumes the vehicle has a **maximum range of 500 miles**, so multiple fuel-ups may need to be shown on the route.
4. Returns the **total money spent on fuel**, assuming the vehicle gets **10 miles per gallon**.
5. Uses the provided **fuel price file** (OPIS truck stop prices).
6. Uses a **free map and routing API**.

Requirements:

- Built with the **latest stable Django**.
- **Fast responses**: the quicker the better.
- **Few calls to the routing API**: one call is ideal, two or three is acceptable.
- A Loom (5 min max) using Postman to show the API working, plus a quick code overview.

## How each requirement is met

| Requirement | How | Where |
|---|---|---|
| Start and finish in the USA | Accepts `"City, ST"`, `{"lat", "lon"}` or `"lat,lon"`. Resolved locally against ~30k US places. Points outside the contiguous US are rejected **before** any routing call | [`locations.py`](trips/services/locations.py), [`geo.py`](trips/services/geo.py) |
| Map of the route | Response includes the route as a GeoJSON `LineString`. `map_url` opens a Leaflet map with the route, numbered stops and a cost breakdown | [`planner.py`](trips/services/planner.py), [`map.html`](trips/templates/trips/map.html) |
| Optimal fuel stops | Greedy "fill or not to fill" algorithm, provably cheapest for a fixed route. By default, an exact dynamic programme adds a $5-per-stop penalty, which avoids top-ups that save cents | [`optimizer.py`](trips/services/optimizer.py) |
| 500 mile range, multiple stops | 50 gal tank (500 mi ÷ 10 MPG). No leg ever exceeds 500 mi. A stretch with no station returns `422 INFEASIBLE_ROUTE` with the gap's location | [`optimizer.py`](trips/services/optimizer.py) |
| Total fuel cost at 10 MPG | `total_fuel_cost` is the exact sum of stop costs, using `Decimal`. `trip_fuel_cost_estimate` also prices the fuel used from the starting tank | [`planner.py`](trips/services/planner.py) |
| Provided price file | Imported once with `manage.py import_stations`. Stations are geocoded offline, duplicate IDs collapse to the lowest price, and Canadian rows are skipped | [`import_stations.py`](trips/management/commands/import_stations.py) |
| Free routing API | OSRM public server, with Valhalla as a fallback. No API key needed | [`routing.py`](trips/services/routing.py) |
| Latest stable Django | Django 6.1 on Python 3.12 | [`requirements.txt`](requirements.txt) |
| Fast | About 0.85 s on a cold cache (mostly the OSRM call), about 30 ms when cached. Stations are held in an in-memory numpy index, so there are no DB queries per request | [`station_index.py`](trips/services/station_index.py) |
| Minimal routing calls | **1 call** per new trip, **0** when cached. Each response reports `meta.routing_api_calls`, and the tests assert the count | [`test_api_integration.py`](tests/test_api_integration.py) |
| Postman demo | 11 requests, each with test assertions | [`postman/`](postman/fuel-route-planner.postman_collection.json) |

## Try it

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/route-plan/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

| Trip | Distance | Stops | Fuel bought |
|---|---|---|---|
| Chicago, IL → Dallas, TX | 969 mi | 2 | $135.41 |
| New York, NY → Los Angeles, CA | 2,801 mi | 6 | $702.52 |
| Seattle, WA → Los Angeles, CA | — | — | `422 INFEASIBLE_ROUTE`: no stations in the price file for 886 mi south of Halsey, OR |

Setup, the full API reference, the algorithm and the assumptions are in the [README](README.md).
