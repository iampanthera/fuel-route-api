"""Turn user input into a validated (lat, lon) with zero network calls.

Accepted forms:
  {"lat": 39.7, "lon": -104.9}     coordinates object
  "39.7, -104.9"                   coordinate string
  "Denver, CO" / "Denver CO"       city + state code (or full state name)
  "Hays"                           city alone, if the name is unique in one state
Free-text street addresses are not geocoded (that would add external calls);
they get a clear LOCATION_NOT_FOUND with a hint.
"""
import math
import re
from dataclasses import dataclass

from .errors import InvalidRequest, LocationAmbiguous, LocationNotFound, OutsideServiceArea
from .gazetteer import get_gazetteer
from .geo import in_service_area

MAX_TEXT = 200
_COORD_RE = re.compile(r"^\s*([-+]?\d{1,3}(?:\.\d+)?)\s*,\s*([-+]?\d{1,3}(?:\.\d+)?)\s*$")
STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "district of columbia": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN",
    "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND", "ohio": "OH",
    "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}
STATE_CODES = set(STATE_NAMES.values())


@dataclass(frozen=True)
class ResolvedLocation:
    query: object
    lat: float
    lon: float
    resolved_by: str
    label: str = ""

    def as_dict(self):
        d = {"query": self.query, "lat": round(self.lat, 6), "lon": round(self.lon, 6), "resolved_by": self.resolved_by}
        if self.label:
            d["label"] = self.label
        return d


def _number(value, field, lo, hi):
    if isinstance(value, bool) or value is None:
        raise InvalidRequest(f"'{field}' must be a number", {"field": field})
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise InvalidRequest(f"'{field}' must be a number", {"field": field}) from None
    if not math.isfinite(num) or not lo <= num <= hi:
        raise InvalidRequest(f"'{field}' must be between {lo} and {hi}", {"field": field})
    return num


def _split_city_state(text):
    cleaned = re.sub(r",?\s*(usa|us|united states)\.?$", "", text.strip(), flags=re.I).strip()
    if "," in cleaned:
        city, state = cleaned.rsplit(",", 1)
    else:
        parts = cleaned.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].strip().upper() in STATE_CODES:
            city, state = parts
        else:
            return cleaned, None
    state = state.strip()
    code = state.upper() if state.upper() in STATE_CODES else STATE_NAMES.get(state.lower())
    return city.strip(), code or state


def resolve_location(value, field: str) -> ResolvedLocation:
    if isinstance(value, dict):
        if "lat" not in value or "lon" not in value:
            raise InvalidRequest(f"'{field}' needs both 'lat' and 'lon'", {"field": field})
        lat = _number(value["lat"], f"{field}.lat", -90, 90)
        lon = _number(value["lon"], f"{field}.lon", -180, 180)
        return ResolvedLocation({"lat": lat, "lon": lon}, lat, lon, "coordinates")

    if not isinstance(value, str):
        raise InvalidRequest(f"'{field}' must be a place name or a {{lat, lon}} object", {"field": field})
    text = value.strip()
    if not text:
        raise InvalidRequest(f"'{field}' is empty", {"field": field})
    if len(text) > MAX_TEXT:
        raise InvalidRequest(f"'{field}' is longer than {MAX_TEXT} characters", {"field": field})

    m = _COORD_RE.match(text)
    if m:
        lat = _number(m.group(1), f"{field}.lat", -90, 90)
        lon = _number(m.group(2), f"{field}.lon", -180, 180)
        return ResolvedLocation(text, lat, lon, "coordinates")

    gaz = get_gazetteer()
    city, state = _split_city_state(text)
    if state:
        if state not in STATE_CODES:
            raise LocationNotFound(f"Unknown US state '{state}' in {field}", {"field": field, "query": text})
        places = gaz.lookup(city, state)
        if not places:
            raise LocationNotFound(
                f"Could not find '{city}, {state}'. Use 'City, ST' for a US town or 'lat,lon' coordinates.",
                {"field": field, "query": text},
            )
        p = places[0]
        return ResolvedLocation(text, p.lat, p.lon, "gazetteer", f"{p.city}, {p.state}")

    places = gaz.lookup_any_state(city)
    states = sorted({p.state for p in places})
    if len(states) == 1:
        p = places[0]
        return ResolvedLocation(text, p.lat, p.lon, "gazetteer", f"{p.city}, {p.state}")
    if len(states) > 1:
        raise LocationAmbiguous(
            f"'{city}' exists in {len(states)} states; add a state, e.g. '{city}, {states[0]}'",
            {"field": field, "candidates": [{"city": city, "state": s} for s in states[:25]]},
        )
    raise LocationNotFound(
        f"Could not find '{text}'. Street addresses are not geocoded; use 'City, ST' or 'lat,lon'.",
        {"field": field, "query": text},
    )


def ensure_in_service_area(loc: ResolvedLocation, field: str):
    if not in_service_area(loc.lat, loc.lon):
        raise OutsideServiceArea(
            f"'{field}' is outside the service area (contiguous United States)",
            {"field": field, "lat": loc.lat, "lon": loc.lon},
        )
