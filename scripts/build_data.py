"""Rebuild the two static data files in data/ (run once; outputs are committed).

1. data/us_places_gazetteer.csv  (state, city, lat, lon)
   Source: kelvins/US-Cities-Database (MIT), about 29k US places.
2. data/lower48.geojson           (simplified, slightly buffered lower-48 + DC outline)
   Source: PublicaMundi/MappingAPI us-states.json (US Census derived).

Usage:  python scripts/build_data.py
"""
import csv
import io
import json
from pathlib import Path

import requests
from shapely.geometry import mapping, shape
from shapely.ops import unary_union

DATA = Path(__file__).resolve().parent.parent / "data"
CITIES_URL = "https://raw.githubusercontent.com/kelvins/US-Cities-Database/main/csv/us_cities.csv"
STATES_URL = "https://raw.githubusercontent.com/PublicaMundi/MappingAPI/master/data/geojson/us-states.json"
EXCLUDED_STATES = {"Alaska", "Hawaii", "Puerto Rico"}
# Places the OPIS file uses that the source dataset lacks (approximate centroids).
MANUAL_PLACES = [
    ("GA", "Port Wentworth", 32.14910, -81.16317),
    ("NJ", "Elizabethport", 40.64816, -74.18792),
    ("AL", "Evergreen", 31.43350, -86.95692),
    ("VA", "Henrico", 37.55064, -77.46091),
    ("IL", "University Park", 41.44337, -87.68338),
]
BUFFER_DEGREES = 0.05  # about 5 km, so border towns survive the simplification


def build_gazetteer():
    text = requests.get(CITIES_URL, timeout=60).text
    rows = csv.DictReader(io.StringIO(text))
    seen = set()
    out = []
    for r in rows:
        key = (r["STATE_CODE"], r["CITY"], r["LATITUDE"], r["LONGITUDE"])
        if key in seen:
            continue
        seen.add(key)
        out.append([r["STATE_CODE"], r["CITY"], round(float(r["LATITUDE"]), 5), round(float(r["LONGITUDE"]), 5)])
    out.extend(list(p) for p in MANUAL_PLACES)
    out.sort()
    with open(DATA / "us_places_gazetteer.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state", "city", "lat", "lon"])
        w.writerows(out)
    print(f"gazetteer: {len(out)} places")


def build_lower48():
    fc = requests.get(STATES_URL, timeout=60).json()
    shapes = [shape(f["geometry"]) for f in fc["features"] if f["properties"]["name"] not in EXCLUDED_STATES]
    outline = unary_union(shapes).buffer(BUFFER_DEGREES).simplify(0.02, preserve_topology=True)
    with open(DATA / "lower48.geojson", "w") as fh:
        json.dump({"type": "Feature", "properties": {"name": "lower48+DC"}, "geometry": mapping(outline)}, fh)
    print(f"lower48: {len(shapes)} states, area {outline.area:.1f} sq deg")


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    build_gazetteer()
    build_lower48()
