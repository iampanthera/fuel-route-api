# Fuel Route Planner API

A Django 6.1 API that takes a start and finish inside the USA and returns the driving route, the cheapest places to refuel along it (500 mi range, 10 MPG), and the total fuel cost. It also gives you a link to a map page.

**[The assessment brief, and how each requirement is met →](ASSESSMENT.md)**

- **One routing call per trip.** Zero when the route is cached or start equals finish.
- **Free, open-source routing, no API key.** OSRM is the primary server and Valhalla the automatic fallback.
- **Fast.** About 30 to 100 ms of server time on top of the routing call, even on 3,000 mile trips.
- **499 automated tests.** They cover the optimiser (checked against a brute-force solver), the CSV import, and the full HTTP API with the routing servers mocked.

---

## Quick start

Needs **Python 3.12+** (Django 6.1 requirement).

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python manage.py migrate
python manage.py import_stations data/fuel-prices-for-be-assessment.csv
python manage.py runserver
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/route-plan/ \
  -H "Content-Type: application/json" \
  -d '{"start": "Chicago, IL", "finish": "Dallas, TX"}'
```

Open the `map_url` from the response in a browser to see the route and stops. There's also a small form at `http://127.0.0.1:8000/`.

**Postman:** import `postman/fuel-route-planner.postman_collection.json`. It has 11 requests, each with assertions. Request 2 saves the map URL that request 11 opens.

**Tests:**

```bash
pytest                 # 499 tests, fully offline (routing servers mocked)
pytest -m live         # 5 smoke tests against the real public OSRM/Valhalla servers
```

---

## API

### `POST /api/v1/route-plan/` (or `GET ?start=..&finish=..`)

```json
{ "start": "Chicago, IL", "finish": {"lat": 32.7767, "lon": -96.797}, "stop_penalty_usd": 5 }
```

`start` and `finish` can be:

| Form | Example | External calls |
|---|---|---|
| City and state | `"Denver, CO"`, `"Denver CO"`, `"Denver, Colorado"` | 0 (local gazetteer of ~30k US places) |
| City alone, if unique | `"Hays"` | 0 |
| Coordinates | `{"lat": 39.74, "lon": -104.99}` or `"39.74,-104.99"` | 0 |

`stop_penalty_usd` is optional (0 to 1000, default 5). See [Choosing stops](#choosing-stops).

### Response (trimmed)

```json
{
  "start":  {"query": "Chicago, IL", "lat": 41.88585, "lon": -87.61812, "resolved_by": "gazetteer", "label": "Chicago, IL"},
  "finish": {"query": "Dallas, TX", "lat": 32.79044, "lon": -96.80439, "resolved_by": "gazetteer", "label": "Dallas, TX"},
  "route": {"distance_miles": 925.8, "duration_hours": 13.9, "provider": "osrm",
            "geometry": {"type": "LineString", "coordinates": [[-87.61812, 41.88585], "..."]}},
  "fuel_stops": [
    {"sequence": 1, "station_id": 67138, "name": "Kum N Go #0561", "address": "US-65 & SR-744",
     "city": "Springfield", "state": "MO", "lat": 37.21, "lon": -93.29,
     "route_mile": 495.4, "offset_miles": 0.0,
     "price_per_gallon": "2.899", "gallons": "42.580", "cost": "123.44"}
  ],
  "summary": {
    "total_fuel_cost": "123.44",
    "gallons_purchased": "42.580",
    "gallons_consumed": "92.580",
    "starting_tank_gallons_used": "50.000",
    "route_median_price_per_gallon": "3.299",
    "trip_fuel_cost_estimate": "288.39",
    "stations_considered": 194,
    "vehicle": {"range_miles": 500.0, "mpg": 10.0, "tank_gallons": 50.0},
    "stop_penalty_usd": 5.0,
    "assumptions": ["Vehicle starts with a full, already-paid 50 gal tank; ...", "..."]
  },
  "map_url": "http://127.0.0.1:8000/api/v1/route-plan/11d4fa53-.../map/",
  "meta": {"routing_api_calls": 1, "geocoding_api_calls": 0, "cache_hit": false, "elapsed_ms": 41.7}
}
```

`total_fuel_cost` is what you pay at the pumps on this trip. The vehicle starts with a full tank that is already paid for, so a trip under 500 mi costs $0.00 here. `trip_fuel_cost_estimate` is the value of **all** the fuel burned: it adds the fuel used from the starting tank, priced at the median price of the stations along the route. It is `null` if no station is near the route.

Money is returned as strings with exact decimal values. The total always equals the sum of the stop costs, and each stop's cost equals price × gallons, rounded to cents.

### Errors

Every error has the same shape: `{"error": {"code", "message", "details"}}`.

| Status | Code | When |
|---|---|---|
| 400 | `INVALID_REQUEST` | Bad JSON, missing field, lat/lon out of range, NaN, wrong types, text over 200 chars |
| 400 | `LOCATION_AMBIGUOUS` | e.g. `"Springfield"`. Details list the candidate states |
| 405 | | PUT / PATCH / DELETE |
| 422 | `LOCATION_NOT_FOUND` | Unknown place or unknown state code |
| 422 | `LOCATION_OUTSIDE_SERVICE_AREA` | Canada, Mexico, ocean, Alaska, Hawaii. Checked **before** any routing call |
| 422 | `INFEASIBLE_ROUTE` | A stretch of more than 500 mi has no station in the price file. Details give the gap's miles and the nearest towns |
| 422 | `NO_ROUTE` / `ROUTE_TOO_LONG` | Reported by the routing server |
| 502 / 503 / 504 | `ROUTING_PROVIDER_ERROR` / `ROUTING_RATE_LIMITED` / `ROUTING_TIMEOUT` | Both routing servers failed. Details list each attempt |

### `GET /api/v1/route-plan/<plan_id>/map/`

A Leaflet map with the route, numbered stops and a cost summary. It's rendered from the cached plan, so it makes **no routing call**.

---

## How it works

```
request ─► resolve start/finish locally ─► lower-48 check ─► route cache? ──yes──┐
                                                               │ no            │
                                                               ▼               │
                                              OSRM (1 call) ─fail─► Valhalla   │
                                                               │               │
                                                               ▼               ▼
                        stations within 10 mi of the route (in-memory index, numpy)
                                                               │
                                                               ▼
                                   optimiser ─► response + stored plan for the map page
```

1. **Stations are geocoded once, offline.** The CSV has no coordinates, and addresses like `I-44, EXIT 283 & US-69` can't be geocoded. `import_stations` matches each station's city and state against a bundled gazetteer of ~30k US places. City names are normalised first (`Mc Graw` = `McGraw`, `Saint Johns` = `St. Johns`), and **100% of US stations match**.
2. **Routing** uses the public OSRM server: one GET with `overview=full&geometries=geojson`. If OSRM times out, returns a 5xx or rate-limits, Valhalla is tried once. "No route exists" is final and never retried.
3. **Corridor matching.** Mile markers are computed along the route polyline and scaled to the server's road distance. Every station within `FUEL_CORRIDOR_MILES` (10) of the route is projected onto it, giving a `route_mile` and an `offset_miles`. This uses two vectorised numpy passes: a coarse filter, then exact point-to-segment distances.
4. **Optimiser.** See below.
5. **Caching.** The **route** is cached for 6 h, keyed on coordinates rounded to 4 decimals (about 10 m). The **fuel plan** is recomputed each time, which takes milliseconds, so new prices apply at once without spending another routing call.

### Choosing stops

- With `stop_penalty_usd = 0` the API returns the **provably cheapest plan** for the route, using the classic greedy "fill or not to fill" rule (Khuller, Malekian, Mestre 2007):
  - If a cheaper station is within range, buy just enough to reach it.
  - Otherwise, if the destination is within range, buy just enough to finish.
  - Otherwise, fill up and head to the cheapest station in range.
- **The pure optimum is often silly in practice.** It will stop for 0.8 gal to save 3 cents. So by default each extra stop is weighed as **$5** when *choosing* stops (think driver time). That dollar amount is never added to the reported fuel cost. With a penalty, an exact dynamic programme over tank levels (0.01 gal steps) is used.
- On simulated cross-country trips, the $5 default cut stops from 13 to 7 (NYC to LA) and from 20 to 8 (Seattle to Miami), for $2 to $4 more fuel.

---

## Assumptions and trade-offs

The main judgement calls, and why.

1. **The vehicle starts with a full, already-paid tank and arrives empty.** Total cost = fuel bought on the way. A trip under 500 mi therefore costs $0.00. The response says so in `assumptions`, and `trip_fuel_cost_estimate` also prices the fuel used from the starting tank.
2. **Stations are placed at their city's centre.** That's why the corridor is 10 mi wide. `offset_miles` is shown but the detour isn't charged, because with city-level coordinates any detour figure would be a guess.
3. **Duplicate rows for one station ID** (905 in the file) collapse to the **lowest** price.
4. **Stops are optimised along the fastest route only.** Searching alternative routes would cost more routing calls, which the brief penalises.
5. **Some trips are infeasible because of the data, not the code.** California's only stations are in the Imperial and Coachella valleys, so Seattle to LA on I-5 has an 800+ mi gap. That returns 422 `INFEASIBLE_ROUTE` with the location, never a silently wrong plan.
6. **Free-text street addresses aren't geocoded.** Geocoding them would add external calls and a rate-limited dependency. Coordinates and `City, ST` cover the brief with zero calls.
7. **Public routing servers have no uptime guarantee** and ask for fair, non-commercial use (about 1 request/s). The fallback and the cache soften this. For production you'd run OSRM or Valhalla yourself in Docker; only `OSRM_BASE_URL` / `VALHALLA_BASE_URL` would change.
8. **OSRM's public server routes cars, not trucks.** Setting `VALHALLA_COSTING=truck` and `ROUTING_PROVIDERS=valhalla` gives truck-legal routing.

## Data notes (from the provided CSV)

| | |
|---|---|
| Rows | 8,151 |
| US stations after dedupe | 6,626 |
| Canadian rows skipped | 620 |
| Duplicate IDs collapsed | 905 |
| Unmatched cities | 0 |
| Ambiguous city names | 319 (same name twice in one state). The candidate nearest the other stations on the same rack (fuel terminal) wins. This fixed Orlando, FL, which the gazetteer also lists at Cape Canaveral, 48 mi away |

- **Encoding.** One station name was double-encoded UTF-8 (`Stuckeyâ€™s`). It's repaired on import.
- **Outliers** such as $6.039 (Phoenix) and $6.399 (Jacumba, CA) are kept. The optimiser simply avoids them.

## Configuration (env vars)

| Variable | Default | |
|---|---|---|
| `ROUTING_PROVIDERS` | `osrm,valhalla` | Order = fallback order |
| `OSRM_BASE_URL` | `https://router.project-osrm.org` | Point at a self-hosted OSRM |
| `VALHALLA_BASE_URL` | `https://valhalla1.openstreetmap.de` | |
| `VALHALLA_COSTING` | `auto` | `truck` for HGV routing |
| `ROUTING_TIMEOUT_SECONDS` | `8` | No automatic retries |
| `VEHICLE_RANGE_MILES` / `VEHICLE_MPG` | `500` / `10` | |
| `FUEL_CORRIDOR_MILES` | `10` | |
| `STOP_PENALTY_USD` | `5` | Default for `stop_penalty_usd` |
| `ROUTE_CACHE_SECONDS` | `21600` | |

| `CACHE_DIR` | `./.cache` | File cache for routes and plans. It survives restarts and is shared by all workers on one machine |
| `REDIS_URL` | unset | Use Redis instead, to share the cache across machines (`pip install redis`) |
| `DATABASE_URL` | unset (SQLite) | `postgres://user:pass@host:5432/db` (`pip install "psycopg[binary]"`) |

## Docker

```bash
docker build -t fuel-route-api .
docker run -p 8000:8000 fuel-route-api      # migrates, imports the CSV, serves with gunicorn
```

The image includes gunicorn, psycopg and redis, so `DATABASE_URL` and `REDIS_URL` work as-is.

## Project layout

```
fuelroute/                  settings, urls, wsgi (warms the station index at startup)
trips/
  models.py                 FuelStation
  management/commands/
    import_stations.py      CSV -> DB, dedupe, encoding repair, offline geocoding
  services/
    locations.py            input parsing + validation (coords, "City, ST")
    gazetteer.py            local US place lookup with name normalisation
    geo.py                  distances, route projection, lower-48 check, polyline decoding
    routing.py              OSRM + Valhalla clients, error mapping, fallback
    station_index.py        in-memory station arrays + corridor search
    optimizer.py            greedy optimum + stop-penalty DP
    planner.py              orchestration, caching, response building
  views.py                  JSON API + map page
  templates/trips/          map.html (Leaflet, vendored), index.html
data/                       price CSV, gazetteer, lower-48 outline
scripts/build_data.py       rebuilds the gazetteer and outline from their sources
postman/                    Postman collection
tests/                      optimizer, import and API integration tests
```

## Data sources

- **US places:** [kelvins/US-Cities-Database](https://github.com/kelvins/US-Cities-Database) (MIT), plus 5 manually added places.
- **State outlines:** [PublicaMundi/MappingAPI](https://github.com/PublicaMundi/MappingAPI) `us-states.json` (US Census derived).
- **Map library:** Leaflet 1.9.4 (BSD-2), vendored. Map tiles © OpenStreetMap contributors.
