"""Load the OPIS fuel price CSV into FuelStation rows and geocode them offline.

    python manage.py import_stations data/fuel-prices-for-be-assessment.csv

Handles every quirk found in the real file: comma or tab delimiters, duplicate
IDs (lowest price wins), Canadian rows, bad prices, broken encodings, stray
whitespace, and city spellings that differ from the gazetteer.
"""
import csv
import io
import logging
import re
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from trips.models import FuelStation
from trips.services.gazetteer import get_gazetteer
from trips.services.geo import haversine_miles
from trips.services.station_index import StationIndex

log = logging.getLogger("trips.import")

US_STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
COL_ID, COL_NAME, COL_ADDR, COL_CITY, COL_STATE, COL_RACK, COL_PRICE = (
    "OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price",
)
REQUIRED = [COL_ID, COL_NAME, COL_CITY, COL_STATE, COL_PRICE]
_SPACES = re.compile(r"\s+")


def decode_bytes(raw: bytes) -> str:
    """UTF-8 when possible; otherwise decode line by line with a cp1252 fallback."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        lines = []
        for line in raw.split(b"\n"):
            try:
                lines.append(line.decode("utf-8"))
            except UnicodeDecodeError:
                lines.append(line.decode("cp1252", errors="replace"))
        return "\n".join(lines).lstrip("﻿")


def repair_mojibake(text: str) -> str:
    """Undo double-encoded UTF-8 (e.g. 'StuckeyÃ¢â‚¬â„¢s' -> "Stuckey's")."""
    for _ in range(3):
        try:
            fixed = text.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if fixed == text:
            break
        text = fixed
    return text.replace("’", "'").replace("‘", "'")


def clean(value) -> str:
    return _SPACES.sub(" ", repair_mojibake(value or "")).strip()


def parse_price(value) -> Decimal | None:
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price.quantize(Decimal("0.0001"))


class Command(BaseCommand):
    help = "Import OPIS fuel prices from CSV and geocode stations to city level."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--gazetteer", default=None, help="Override the gazetteer CSV path")
        parser.add_argument("--no-prune", action="store_true", help="Keep stations missing from this file")

    def handle(self, *args, csv_path, gazetteer=None, no_prune=False, **opts):
        path = Path(csv_path)
        if not path.exists():
            raise CommandError(f"File not found: {path}")
        text = decode_bytes(path.read_bytes())
        sample = text[:5000]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        reader.fieldnames = headers
        missing = [c for c in REQUIRED if c not in headers]
        if missing:
            raise CommandError(f"Missing required column(s): {', '.join(missing)}")

        stats = dict(rows=0, skipped_non_us=0, skipped_bad_price=0, skipped_bad_id=0, duplicates_collapsed=0)
        best: dict[int, dict] = {}
        for row in reader:
            stats["rows"] += 1
            state = clean(row.get(COL_STATE)).upper()
            if state not in US_STATES:
                stats["skipped_non_us"] += 1
                continue
            price = parse_price(row.get(COL_PRICE))
            if price is None:
                stats["skipped_bad_price"] += 1
                continue
            try:
                opis_id = int(clean(row.get(COL_ID)))
            except ValueError:
                stats["skipped_bad_id"] += 1
                continue
            rack = clean(row.get(COL_RACK))
            record = {
                "opis_id": opis_id,
                "name": clean(row.get(COL_NAME))[:200],
                "address": clean(row.get(COL_ADDR)).strip(" ,")[:300],
                "city": clean(row.get(COL_CITY))[:100],
                "state": state,
                "rack_id": int(rack) if rack.isdigit() else None,
                "retail_price": price,
            }
            if opis_id in best:
                stats["duplicates_collapsed"] += 1
                if price < best[opis_id]["retail_price"]:
                    best[opis_id] = record
            else:
                best[opis_id] = record

        gaz = get_gazetteer(gazetteer)
        unmatched, ambiguous = [], 0
        matches = {oid: gaz.lookup(rec["city"], rec["state"]) for oid, rec in best.items()}
        # Where one name maps to several places in a state (the gazetteer has two
        # "Orlando, FL" rows, one at Cape Canaveral), pick the candidate nearest the
        # stations that share the same rack (regional fuel terminal).
        rack_points: dict[int, list[tuple[float, float]]] = {}
        for oid, places in matches.items():
            if len(places) == 1 and best[oid]["rack_id"] is not None:
                rack_points.setdefault(best[oid]["rack_id"], []).append((places[0].lat, places[0].lon))
        for oid, rec in best.items():
            places = matches[oid]
            if places:
                place = places[0]
                if len(places) > 1 and rack_points.get(rec["rack_id"]):
                    lat0 = statistics.median(p[0] for p in rack_points[rec["rack_id"]])
                    lon0 = statistics.median(p[1] for p in rack_points[rec["rack_id"]])
                    place = min(places, key=lambda p: float(haversine_miles(lat0, lon0, p.lat, p.lon)))
                rec["lat"], rec["lon"] = place.lat, place.lon
                rec["geocode_source"] = "gazetteer"
                rec["geocode_precision"] = "city" if len(places) == 1 else "city_ambiguous"
                ambiguous += len(places) > 1
            else:
                rec["lat"] = rec["lon"] = None
                rec["geocode_source"] = ""
                rec["geocode_precision"] = "none"
                unmatched.append(f"{rec['city']}, {rec['state']}")

        prices = [float(r["retail_price"]) for r in best.values()]
        outliers = []
        if len(prices) > 2:
            mean, sd = statistics.mean(prices), statistics.pstdev(prices)
            outliers = [r for r in best.values() if sd and float(r["retail_price"]) > mean + 3 * sd]

        fields = ["name", "address", "city", "state", "rack_id", "retail_price", "lat", "lon",
                  "geocode_source", "geocode_precision"]
        with transaction.atomic():
            FuelStation.objects.bulk_create(
                [FuelStation(**r) for r in best.values()],
                update_conflicts=True,
                unique_fields=["opis_id"],
                update_fields=fields,
                batch_size=500,
            )
            pruned = 0
            if not no_prune:
                pruned, _ = FuelStation.objects.exclude(opis_id__in=list(best)).delete()
        StationIndex.reset()

        for o in outliers:
            log.debug("Price outlier kept: %s %s %s", o["opis_id"], o["name"], o["retail_price"])
        if unmatched:
            log.warning("Unmatched cities (stored without coordinates): %s", sorted(set(unmatched)))
        self.stdout.write(
            "imported={imported} rows={rows} duplicates_collapsed={duplicates_collapsed} "
            "skipped_non_us={skipped_non_us} skipped_bad_price={skipped_bad_price} "
            "skipped_bad_id={skipped_bad_id} unmatched_city={unmatched} ambiguous_city={ambiguous} "
            "outliers={outliers} pruned={pruned}".format(
                imported=len(best), unmatched=len(unmatched), ambiguous=ambiguous,
                outliers=len(outliers), pruned=pruned, **stats,
            )
        )
