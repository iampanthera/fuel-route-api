"""Integration tests for `manage.py import_stations`.

Runs the real management command against a real (test) database and a small
local gazetteer. Remote geocoding is disabled, so any network access fails the test.
Each CSV row below reproduces a quirk found in the actual OPIS file.
"""
import io
from decimal import Decimal

import pytest
from django.core.management import call_command

from trips.models import FuelStation
from trips.services.station_index import StationIndex

from .conftest import FIXTURES

pytestmark = pytest.mark.django_db

HEADER = "OPIS Truckstop ID,Truckstop Name,Address,City,State,Rack ID,Retail Price\n"

ROWS = [
    # plain row
    '7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333',
    # same ID, several prices -> keep the min (3.269)
    '105,TA SAGINAW I 75 TRAVEL CENTER,"I-75, EXIT 144-B",Bridgeport,MI,260,3.339',
    '105,TA SAGINAW I 75 TRAVEL CENTER,"I-75, EXIT 144-B",Bridgeport,MI,260,3.269',
    '105,TA SAGINAW I 75 TRAVEL CENTER,"I-75, EXIT 144-B",Bridgeport,MI,260,3.429',
    # same ID, different names, same price -> one station
    '20,PILOT TRAVEL CENTER #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899',
    '20,PILOT #1243,"I-8, EXIT 119 & SR-85",Gila Bend,AZ,930,3.899',
    # Canadian province -> skipped
    "629,FLYING J #850,TCH-16,Edmonton,AB,80,4.39948962",
    '52493,10 ACRE TRUCKSTOP,"HWY-401, EXIT 53B & HWY 1",Belleville,ON,420,3.45750874',
    # city spelling differs from gazetteer ("Mc Graw" vs "McGraw", "Saint" vs "St.")
    '1599,ONVO TRAVEL PLAZA,"I-81, EXIT 10",Mc Graw,NY,220,3.439',
    '341,TA JACKSONVILLE SOUTH TRAVEL CENTER,"I-95, EXIT 329",Saint Johns,FL,75,3.469',
    # trailing whitespace inside fields
    '5780,BILLS TRUCKSTOP ,"I-85, EXIT 86                 ",Linwood ,NC,65,3.449',
    # price outlier -> kept
    '70300,PILOT #1194,"I-10 & US-60",Phoenix,AZ,930,6.039',
    # bad prices -> skipped
    '99999,ZERO PRICE STOP,"I-1, EXIT 1",Big Cabin,OK,1,0',
    '99998,NEGATIVE PRICE STOP,"I-1, EXIT 2",Big Cabin,OK,1,-3.10',
    '99997,TEXT PRICE STOP,"I-1, EXIT 3",Big Cabin,OK,1,abc',
    # city not in gazetteer -> stored ungeocoded, excluded from index
    '88888,NOWHERE STOP,US-1,Atlantis,FL,75,3.10',
    # nearly empty address (ID 70745 in the real file has just ",")
    '70745,CASEYS #3752,",",Big Cabin,OK,425,3.40233333',
]


def write_csv(tmp_path, rows=ROWS, delimiter=",", name="stations.csv", extra_bytes=b""):
    text = HEADER + "\n".join(rows) + "\n"
    if delimiter == "\t":
        import csv

        out = io.StringIO()
        reader = csv.reader(io.StringIO(text))
        writer = csv.writer(out, delimiter="\t", lineterminator="\n")
        for r in reader:
            writer.writerow(r)
        text = out.getvalue()
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8") + extra_bytes)
    return path


def run_import(path, **opts):
    out = io.StringIO()
    call_command(
        "import_stations",
        str(path),
        gazetteer=str(FIXTURES / "gazetteer_sample.csv"),
        stdout=out,
        **opts,
    )
    return out.getvalue()


# --------------------------------------------------------------------------- #
def test_imports_and_reports_summary(tmp_path):
    output = run_import(write_csv(tmp_path))

    # 7, 105, 20, 1599, 341, 5780, 70300, 88888, 70745 are US stations with valid prices
    assert FuelStation.objects.count() == 9
    assert "skipped_non_us=2" in output
    assert "skipped_bad_price=3" in output
    assert "unmatched_city=1" in output


def test_duplicate_ids_collapse_to_lowest_price(tmp_path):
    run_import(write_csv(tmp_path))
    station = FuelStation.objects.get(opis_id=105)
    assert station.retail_price == Decimal("3.2690")


def test_duplicate_ids_with_name_variants_create_one_station(tmp_path):
    run_import(write_csv(tmp_path))
    assert FuelStation.objects.filter(opis_id=20).count() == 1


def test_canadian_rows_are_skipped(tmp_path):
    run_import(write_csv(tmp_path))
    assert not FuelStation.objects.filter(state__in=["AB", "ON", "BC", "MB", "SK", "QC", "NB", "NS", "YT"]).exists()


def test_bad_prices_are_skipped(tmp_path):
    run_import(write_csv(tmp_path))
    assert not FuelStation.objects.filter(opis_id__in=[99999, 99998, 99997]).exists()


def test_price_outlier_is_kept(tmp_path):
    run_import(write_csv(tmp_path))
    assert FuelStation.objects.get(opis_id=70300).retail_price == Decimal("6.0390")


def test_city_names_are_normalised_for_gazetteer_match(tmp_path):
    run_import(write_csv(tmp_path))
    mcgraw = FuelStation.objects.get(opis_id=1599)
    st_johns = FuelStation.objects.get(opis_id=341)
    assert mcgraw.lat == pytest.approx(42.5934, abs=1e-3)
    assert st_johns.lat == pytest.approx(30.0816, abs=1e-3)
    assert mcgraw.geocode_source == "gazetteer"
    assert mcgraw.geocode_precision == "city"


def test_whitespace_is_stripped(tmp_path):
    run_import(write_csv(tmp_path))
    s = FuelStation.objects.get(opis_id=5780)
    assert s.name == "BILLS TRUCKSTOP"
    assert s.address == "I-85, EXIT 86"
    assert s.city == "Linwood"
    assert s.lat is not None


def test_unmatched_city_is_stored_without_coordinates_and_excluded_from_index(tmp_path):
    run_import(write_csv(tmp_path))
    s = FuelStation.objects.get(opis_id=88888)
    assert s.lat is None and s.lon is None
    index_ids = {st.opis_id for st in StationIndex.get().all_stations()}
    assert 88888 not in index_ids
    assert 7 in index_ids


def test_near_empty_address_does_not_break_import(tmp_path):
    run_import(write_csv(tmp_path))
    assert FuelStation.objects.filter(opis_id=70745).exists()


def test_tab_delimited_file_is_detected(tmp_path):
    run_import(write_csv(tmp_path, delimiter="\t", name="stations.tsv"))
    assert FuelStation.objects.count() == 9
    assert FuelStation.objects.get(opis_id=105).retail_price == Decimal("3.2690")


def test_invalid_utf8_bytes_do_not_crash_import(tmp_path):
    # ID 71108 in the real file is "Stuckey<garbage>s Travel Center West".
    # \x92 is a cp1252 right single quote that is not valid UTF-8.
    bad_row = (
        b'71108,Stuckey\x92s Travel Center West,"I-10, EXIT 819 & FM-1663",'
        b"Anahuac,TX,610,2.80733333\n"
    )
    path = write_csv(tmp_path, extra_bytes=bad_row)
    run_import(path)
    s = FuelStation.objects.get(opis_id=71108)
    assert s.name.startswith("Stuckey")
    assert s.name.endswith("s Travel Center West")
    assert s.lat is not None


def test_reimport_is_idempotent(tmp_path):
    path = write_csv(tmp_path)
    run_import(path)
    run_import(path)
    assert FuelStation.objects.count() == 9


def test_reimport_updates_changed_prices(tmp_path):
    run_import(write_csv(tmp_path))
    changed = [r.replace("3.00733333", "2.50000000") for r in ROWS]
    run_import(write_csv(tmp_path, rows=changed, name="stations_v2.csv"))
    assert FuelStation.objects.get(opis_id=7).retail_price == Decimal("2.5000")


def test_import_resets_the_in_memory_station_index(tmp_path):
    # Build the index while the DB is empty, then import: the index must refresh.
    assert StationIndex.get().all_stations() == []
    run_import(write_csv(tmp_path))
    assert len(StationIndex.get().all_stations()) == 8  # 9 stored minus 1 ungeocoded


def test_missing_required_column_fails_clearly(tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("OPIS Truckstop ID,Truckstop Name,City,State\n7,X,Big Cabin,OK\n")
    with pytest.raises(Exception) as exc:
        run_import(path)
    assert "Retail Price" in str(exc.value)


def test_import_never_calls_network_by_default(tmp_path, mocked_http):
    run_import(write_csv(tmp_path))
    assert len(mocked_http.calls) == 0


def test_ambiguous_city_uses_rack_neighbours(tmp_path):
    # Two "Orlando, FL" places exist (the real gazetteer also lists one at Cape
    # Canaveral). Stations on the same rack sit near the real city, so that wins.
    gaz = tmp_path / "gaz.csv"
    gaz.write_text(
        "state,city,lat,lon\n"
        "FL,Orlando,28.49882,-80.58248\n"
        "FL,Orlando,28.53988,-81.37267\n"
        "FL,Kissimmee,28.2920,-81.4076\n"
        "FL,Ocala,29.1872,-82.1401\n"
    )
    csv_path = write_csv(tmp_path, rows=[
        '3444,ACME TRUCK STOP,US-441,Orlando,FL,140,3.449',
        '63876,MOBIL,"FL TURNPIKE, EXIT 244",Kissimmee,FL,140,3.449',
        '4909,CIRCLE K,US-441,Ocala,FL,140,3.459',
    ])
    call_command("import_stations", str(csv_path), gazetteer=str(gaz), stdout=io.StringIO())

    orlando = FuelStation.objects.get(opis_id=3444)
    assert orlando.geocode_precision == "city_ambiguous"
    assert orlando.lon == pytest.approx(-81.37267)


def test_ambiguous_city_without_rack_neighbours_falls_back_to_first(tmp_path):
    gaz = tmp_path / "gaz.csv"
    gaz.write_text("state,city,lat,lon\nFL,Orlando,28.49882,-80.58248\nFL,Orlando,28.53988,-81.37267\n")
    csv_path = write_csv(tmp_path, rows=['3444,ACME TRUCK STOP,US-441,Orlando,FL,140,3.449'])
    call_command("import_stations", str(csv_path), gazetteer=str(gaz), stdout=io.StringIO())
    assert FuelStation.objects.get(opis_id=3444).lon == pytest.approx(-80.58248)
