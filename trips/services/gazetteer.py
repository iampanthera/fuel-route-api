"""Local US place gazetteer: (city, state) -> (lat, lon) with zero network calls.

City names in the OPIS file and in user input are messy ("Mc Graw", "Saint Johns",
"S Coffeyville", "Sault Sainte Marie"), so both sides are reduced to a
normalised key before matching.
"""
import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from django.conf import settings

_WORD_MAP = {
    "saint": "st",
    "sainte": "ste",
    "mount": "mt",
    "fort": "ft",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "township": "twp",
}
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def normalize_city(name: str) -> str:
    """'Mc Graw' -> 'mcgraw', 'Saint Johns' / 'St. Johns' -> 'stjohns'."""
    words = _NON_ALNUM.sub(" ", name.lower()).split()
    return "".join(_WORD_MAP.get(w, w) for w in words)


@dataclass(frozen=True)
class Place:
    state: str
    city: str
    lat: float
    lon: float


class Gazetteer:
    def __init__(self, path: Path):
        self.by_key: dict[tuple[str, str], list[Place]] = {}
        self.by_city: dict[str, list[Place]] = {}
        self._places = None
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                place = Place(row["state"].strip().upper(), row["city"].strip(), float(row["lat"]), float(row["lon"]))
                key = normalize_city(place.city)
                self.by_key.setdefault((place.state, key), []).append(place)
                self.by_city.setdefault(key, []).append(place)

    def lookup(self, city: str, state: str) -> list[Place]:
        return list(self.by_key.get((state.strip().upper(), normalize_city(city)), []))

    def lookup_any_state(self, city: str) -> list[Place]:
        return list(self.by_city.get(normalize_city(city), []))

    def nearest(self, lat: float, lon: float) -> Place | None:
        """Closest known place (used to describe where a fuel gap is)."""
        if self._places is None:
            self._places = [p for ps in self.by_key.values() for p in ps]
            self._lats = np.array([p.lat for p in self._places])
            self._lons = np.array([p.lon for p in self._places])
        if not self._places:
            return None
        d = (self._lats - lat) ** 2 + ((self._lons - lon) * np.cos(np.radians(lat))) ** 2
        return self._places[int(np.argmin(d))]


@lru_cache(maxsize=4)
def _load(path: str) -> Gazetteer:
    return Gazetteer(Path(path))


def get_gazetteer(path=None) -> Gazetteer:
    return _load(str(path or settings.GAZETTEER_PATH))
