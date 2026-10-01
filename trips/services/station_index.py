"""In-memory index of geocoded stations, built once per process.

About 6.7k stations fit comfortably in a few numpy arrays, so a request never
queries the database. Call StationIndex.reset() after importing new prices
(the import command does this for the current process).
"""
import threading
from dataclasses import dataclass

import numpy as np

from trips.models import FuelStation

from .geo import project_points_on_route


@dataclass(frozen=True)
class CorridorStation:
    station: FuelStation
    route_mile: float
    offset_miles: float


class StationIndex:
    _instance = None
    _lock = threading.Lock()

    def __init__(self, stations: list[FuelStation]):
        self._stations = stations
        self.lats = np.array([s.lat for s in stations], dtype=float)
        self.lons = np.array([s.lon for s in stations], dtype=float)

    @classmethod
    def get(cls) -> "StationIndex":
        inst = cls._instance
        if inst is None:
            with cls._lock:
                if cls._instance is None:
                    stations = list(FuelStation.objects.filter(lat__isnull=False, lon__isnull=False))
                    cls._instance = cls(stations)
                inst = cls._instance
        return inst

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    def all_stations(self) -> list[FuelStation]:
        return list(self._stations)

    def stations_along(self, route_lats, route_lons, route_cum, corridor_miles) -> list[CorridorStation]:
        if not self._stations or len(route_lats) < 2:
            return []
        # Cheap bounding-box prefilter before the exact projection.
        pad_lat = corridor_miles / 69.0 + 0.01
        pad_lon = corridor_miles / 40.0 + 0.01  # 40 mi/deg is safe up to ~54 N
        mask = (
            (self.lats >= route_lats.min() - pad_lat)
            & (self.lats <= route_lats.max() + pad_lat)
            & (self.lons >= route_lons.min() - pad_lon)
            & (self.lons <= route_lons.max() + pad_lon)
        )
        idx = np.nonzero(mask)[0]
        miles, offsets = project_points_on_route(
            route_lats, route_lons, route_cum, self.lats[idx], self.lons[idx], corridor_miles
        )
        out = [
            CorridorStation(self._stations[i], float(m), float(o))
            for i, m, o in zip(idx, miles, offsets)
            if np.isfinite(o)
        ]
        out.sort(key=lambda c: c.route_mile)
        return out
