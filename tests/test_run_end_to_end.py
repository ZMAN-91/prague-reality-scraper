"""End-to-end tests of run.run() against a fake network.

Everything else in this suite tests one function. This file tests the
wiring - fetch -> enrich -> merge -> relist -> cluster -> write - against a
real temporary data directory, because that is where the bugs that actually
cost data live: a field that never reaches the CSV, a run that writes
nothing when a source fails, a second run that duplicates the first run's
observations.
"""

from __future__ import annotations

import csv
import gzip
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import run as run_module
from scrapers import sreality


# --- fake sreality v1 API ------------------------------------------------


def index_payload(items, total=None):
    return {
        "pagination": {"total": total if total is not None else len(items), "limit": 500},
        "results": items,
    }


# The default fixture point is the real Nurmiho coordinate, taken from a
# listing this project actually collected. It has to be inside
# common/collection_area.py or every end-to-end test would be exercising
# the path where sreality discards the listing.
NURMIHO_LAT, NURMIHO_LON = 50.04525, 14.52430
SPORILOV_LAT, SPORILOV_LON = 50.04440, 14.47890  # docs/geo/reference_points.json
PRAGUE_CENTRE = (50.0755, 14.4378)  # inside Prague, outside the collected area


def detail_payload(hash_id, *, lat=NURMIHO_LAT, lon=NURMIHO_LON, price=5_000_000, area=55, sub_cb=4):
    return {
        "result": {
            "hash_id": hash_id,
            "category_main_cb": {"value": 1, "name": "Byt"},
            "category_type_cb": {"value": 1, "name": "Prodej"},
            "category_sub_cb": {"value": sub_cb, "name": "2+kk"},
            "usable_area": area,
            "floor_number": 3,
            "advert_description": "Hezky byt u parku",
            "advert_name": "Prodej bytu 2+kk",
            "locality": {
                "gps_lat": lat,
                "gps_lon": lon,
                "city_seo_name": "praha",
                "citypart_seo_name": "praha-4-sporilov",
                "street_seo_name": "nurniho",
            },
            "price_summary_czk": price,
        },
        "status_code": 200,
    }


class FakeSreality:
    """Serves a fixed set of listing ids; `present` can change between runs
    to simulate listings appearing and disappearing."""

    def __init__(
        self,
        present: list[int],
        price: int = 5_000_000,
        lat: float = NURMIHO_LAT,
        lon: float = NURMIHO_LON,
    ):
        self.present = list(present)
        self.price = price
        self.lat = lat
        self.lon = lon
        self.index_calls = 0
        self.detail_calls: list[str] = []

    def fetch_json(self, session, url, params=None, method="GET", json_body=None,
                   form_data=None, max_retries=5):
        if url == sreality.INDEX_URL:
            self.index_calls += 1
            # Only serve rows for the byt/prodej slice of district 47, so
            # every other slice legitimately comes back empty.
            is_byt_prodej = params.get("category_main_cb") == 1 and params.get("category_type_cb") == 1
            first_district = params.get("locality_district_id") == sreality.DISTRICT_PRAHA
            if is_byt_prodej and first_district and params.get("offset", 0) == 0:
                return index_payload(
                    [{"hash_id": i, "price_czk": self.price} for i in self.present]
                )
            return index_payload([])
        # detail endpoint
        source_id = url.rstrip("/").split("/")[-1]
        self.detail_calls.append(source_id)
        return detail_payload(int(source_id), lat=self.lat, lon=self.lon, price=self.price)


@pytest.fixture
def no_sleep():
    with patch("common.net.polite_sleep", lambda *a, **k: None):
        yield


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "data", tmp_path / "logs"


def do_run(fake, data_dir, logs_dir, **kwargs):
    with patch("common.net.fetch_json", side_effect=fake.fetch_json), \
         patch("common.net.build_session", lambda: object()):
        return run_module.run(["sreality"], data_dir, logs_dir, **kwargs)


def run_over_days(fake, data_dir, logs_dir, days, start=None):
    """One run a day for `days` days. Removal is measured in elapsed days, so
    a test for "gone for a week" has to actually let a week go by; running the
    same minute eight times over proves nothing."""
    from datetime import datetime, timedelta, timezone

    start = start or datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
    for offset in range(days):
        do_run(fake, data_dir, logs_dir, now=start + timedelta(days=offset))


def read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --- the happy path ------------------------------------------------------


def test_single_run_writes_all_three_layers(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[101, 102])
    exit_code = do_run(fake, data_dir, logs_dir)

    assert exit_code == 0
    listings = read_csv_rows(data_dir / "listings.csv")
    assert len(listings) == 2

    row = listings[0]
    # Detail enrichment must have reached the CSV, not just memory.
    assert row["disposition"] == "2+kk"
    assert row["area_m2"] == "55.0"
    assert row["floor"] == "3"
    assert row["description"].startswith("Hezky byt")
    assert row["url"].startswith("https://www.sreality.cz/detail/prodej/byt/2+kk/")
    assert row["status"] == "active"

    observations = [
        r for path in (data_dir / "observations").glob("*.csv") for r in read_csv_rows(path)
    ]
    assert len(observations) == 2
    assert {o["status"] for o in observations} == {"active"}
    assert all(o["price"] == "5000000" for o in observations)

    assert (data_dir / "state" / "last_observation.json").exists()
    assert list(logs_dir.glob("*.jsonl"))


def test_every_listing_field_is_present_in_the_written_csv(no_sleep, paths):
    from common.schema import LISTING_FIELDS

    data_dir, logs_dir = paths
    do_run(FakeSreality(present=[1]), data_dir, logs_dir)
    with open(data_dir / "listings.csv", newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == LISTING_FIELDS


def test_raw_archive_separates_index_and_detail(no_sleep, paths):
    data_dir, logs_dir = paths
    do_run(FakeSreality(present=[1]), data_dir, logs_dir)

    archives = sorted((data_dir / "raw" / "sreality").rglob("*.json.gz"))
    names = [p.name for p in archives]
    assert any(n.startswith("index-") for n in names)
    assert any(n.startswith("detail-") for n in names)

    detail_file = next(p for p in archives if p.name.startswith("detail-"))
    with gzip.open(detail_file, "rt", encoding="utf-8") as f:
        payload = json.load(f)
    # The complete raw detail body is archived, not just the parsed subset.
    assert payload["pages"][0]["response"]["result"]["advert_name"] == "Prodej bytu 2+kk"


# --- run-to-run behaviour ------------------------------------------------


def test_second_identical_run_adds_no_observations_and_no_churn(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1, 2])
    do_run(fake, data_dir, logs_dir)
    first_csv = (data_dir / "listings.csv").read_bytes()
    details_after_first = list(fake.detail_calls)

    do_run(fake, data_dir, logs_dir)
    second_csv = (data_dir / "listings.csv").read_bytes()

    observations = [
        r for path in (data_dir / "observations").glob("*.csv") for r in read_csv_rows(path)
    ]
    assert len(observations) == 2, "an unchanged run must not re-log observations"
    assert fake.detail_calls == details_after_first, "detail is fetched once per listing, ever"
    assert first_csv == second_csv, (
        "an unchanged hourly run must leave listings.csv byte-identical, "
        "otherwise git stores a fresh copy of it every hour"
    )


def test_price_change_is_logged_once(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1])
    do_run(fake, data_dir, logs_dir)

    fake.price = 4_500_000
    do_run(fake, data_dir, logs_dir)
    do_run(fake, data_dir, logs_dir)  # same new price again -> no extra row

    observations = [
        r for path in (data_dir / "observations").glob("*.csv") for r in read_csv_rows(path)
    ]
    prices = [o["price"] for o in observations]
    assert prices == ["5000000", "4500000"]


def test_disappearance_takes_three_days_to_become_removed(no_sleep, paths):
    from datetime import datetime, timedelta, timezone

    data_dir, logs_dir = paths
    start = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    fake = FakeSreality(present=[1, 2])
    do_run(fake, data_dir, logs_dir, now=start)

    fake.present = [1]  # listing 2 disappears
    statuses = []
    for day in range(1, 9):
        do_run(fake, data_dir, logs_dir, now=start + timedelta(days=day))
        rows = {r["source_id"]: r for r in read_csv_rows(data_dir / "listings.csv")}
        statuses.append(rows["2"]["status"])

    assert all(s.startswith("missing_") for s in statuses[:2]), \
        f"removed before the three days were up: {statuses}"
    assert statuses[2] == "removed"
    # ...and the surviving listing is untouched throughout.
    rows = {r["source_id"]: r for r in read_csv_rows(data_dir / "listings.csv")}
    assert rows["1"]["status"] == "active"


def test_source_failure_writes_data_and_does_not_mark_anything_removed(no_sleep, paths):
    """A total outage must not look like every listing being deleted."""
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1, 2])
    do_run(fake, data_dir, logs_dir)

    from common.net import RequestFailed

    def always_fails(*args, **kwargs):
        raise RequestFailed("simulated outage")

    with patch("common.net.fetch_json", side_effect=always_fails), \
         patch("common.net.build_session", lambda: object()):
        exit_code = run_module.run(["sreality"], data_dir, logs_dir)

    assert exit_code == 1  # surfaced as a failure so GitHub emails about it
    rows = read_csv_rows(data_dir / "listings.csv")
    assert len(rows) == 2
    assert {r["status"] for r in rows} == {"active"}, "an outage must never mark listings missing"

    log_lines = [
        json.loads(line)
        for path in logs_dir.glob("*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert log_lines[-1]["ok"] is False
    assert log_lines[-1]["sources"]["sreality"]["errors"]


def test_relisting_links_a_new_ad_to_the_removed_one(no_sleep, paths):
    data_dir, logs_dir = paths
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    fake = FakeSreality(present=[1])
    do_run(fake, data_dir, logs_dir, now=start)
    fake.present = []
    for day in range(1, 9):  # a week of absence, which is what kills it now
        do_run(fake, data_dir, logs_dir, now=start + timedelta(days=day))

    rows = {r["source_id"]: r for r in read_csv_rows(data_dir / "listings.csv")}
    assert rows["1"]["status"] == "removed"

    # A brand-new ad for the same physical unit (same GPS/disposition/area).
    fake.present = [999]
    do_run(fake, data_dir, logs_dir, now=start + timedelta(days=9))

    rows = {r["source_id"]: r for r in read_csv_rows(data_dir / "listings.csv")}
    assert rows["999"]["relisted_from"] == rows["1"]["internal_id"]
    assert rows["1"]["status"] == "removed", "the old row stays, it is not merged away"


# --- budgets -------------------------------------------------------------


def test_detail_budget_limits_new_listings_per_run_without_losing_them(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1, 2, 3, 4, 5])

    do_run(fake, data_dir, logs_dir, max_new_details=2)
    assert len(read_csv_rows(data_dir / "listings.csv")) == 2
    assert len(fake.detail_calls) == 2

    # The rest are not lost - they are simply picked up by later runs.
    do_run(fake, data_dir, logs_dir, max_new_details=2)
    assert len(read_csv_rows(data_dir / "listings.csv")) == 4
    do_run(fake, data_dir, logs_dir, max_new_details=99)
    assert len(read_csv_rows(data_dir / "listings.csv")) == 5


def test_budget_exhaustion_does_not_mark_unseen_listings_missing(no_sleep, paths):
    """Half-finished coverage must never be read as 'these listings are gone'."""
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1, 2, 3])
    do_run(fake, data_dir, logs_dir)
    assert len(read_csv_rows(data_dir / "listings.csv")) == 3

    with patch("common.budget.Budget.time_exhausted", return_value=True):
        do_run(fake, data_dir, logs_dir)

    rows = read_csv_rows(data_dir / "listings.csv")
    assert {r["status"] for r in rows} == {"active"}


def test_run_log_records_the_budget(no_sleep, paths):
    data_dir, logs_dir = paths
    do_run(FakeSreality(present=[1, 2, 3]), data_dir, logs_dir, max_new_details=1)
    entry = json.loads(list(logs_dir.glob("*.jsonl"))[0].read_text(encoding="utf-8").splitlines()[-1])
    assert entry["budget"]["new_details_fetched"] == 1
    assert entry["budget"]["new_details_skipped_for_budget"] == 2


# --- geography -----------------------------------------------------------


def test_listing_outside_the_bounding_box_is_dropped_after_its_detail_fetch(no_sleep, paths):
    """sreality's index walk covers whole districts, which reach well beyond
    the target area, so the bbox check after the detail fetch is the real
    geographic gate."""
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1], lat=49.1951, lon=16.6068)  # Brno
    do_run(fake, data_dir, logs_dir)
    assert read_csv_rows(data_dir / "listings.csv") == []


def test_priority_zone_is_flagged_for_sporilov(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1], lat=SPORILOV_LAT, lon=SPORILOV_LON)
    do_run(fake, data_dir, logs_dir)
    row = read_csv_rows(data_dir / "listings.csv")[0]
    assert row["priority_zone"] == "True"


class FakeSrealityRichIndex(FakeSreality):
    """Serves index rows that already carry everything (as the live API
    actually does), so no detail request should be needed at all."""

    def fetch_json(self, session, url, params=None, method="GET", json_body=None,
                   form_data=None, max_retries=5):
        if url == sreality.INDEX_URL:
            self.index_calls += 1
            is_byt_prodej = params.get("category_main_cb") == 1 and params.get("category_type_cb") == 1
            first_district = params.get("locality_district_id") == sreality.DISTRICT_PRAHA
            if is_byt_prodej and first_district and params.get("offset", 0) == 0:
                return index_payload(
                    [detail_payload(i, lat=self.lat, lon=self.lon, price=self.price)["result"]
                     for i in self.present]
                )
            return index_payload([])
        self.detail_calls.append(url.rstrip("/").split("/")[-1])
        return detail_payload(1)


def test_a_complete_index_row_skips_the_detail_request(no_sleep, paths):
    """The live index already carries locality/category_sub_cb/usable_area,
    so assuming otherwise cost one request per listing - ~12k for Prague, at
    a portal whose robots.txt does not invite that traffic."""
    data_dir, logs_dir = paths
    fake = FakeSrealityRichIndex(present=[1, 2, 3])
    do_run(fake, data_dir, logs_dir)

    assert fake.detail_calls == [], "a complete index row must not trigger a detail fetch"
    rows = read_csv_rows(data_dir / "listings.csv")
    assert len(rows) == 3
    # ...and the data is fully populated anyway.
    assert rows[0]["disposition"] == "2+kk"
    assert rows[0]["area_m2"] == "55.0"
    assert rows[0]["lat"] != ""
    assert rows[0]["url"].startswith("https://www.sreality.cz/detail/prodej/byt/2+kk/")


def test_a_thin_index_row_still_falls_back_to_the_detail_request(no_sleep, paths):
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[1])  # thin rows: hash_id + price only
    do_run(fake, data_dir, logs_dir)
    assert fake.detail_calls == ["1"]
    assert read_csv_rows(data_dir / "listings.csv")[0]["area_m2"] == "55.0"


def test_priority_zone_survives_a_second_run(no_sleep, paths):
    """Regression: once index rows started carrying GPS, an already-known
    listing came back with real coordinates but a default priority_zone of
    False - and merge_source wrote that over the correct stored value,
    silently clearing the Sporilov/Hostivar flag on every listing from the
    second run onwards."""
    data_dir, logs_dir = paths
    fake = FakeSrealityRichIndex(present=[1], lat=SPORILOV_LAT, lon=SPORILOV_LON)

    do_run(fake, data_dir, logs_dir)
    assert read_csv_rows(data_dir / "listings.csv")[0]["priority_zone"] == "True"

    do_run(fake, data_dir, logs_dir)
    assert read_csv_rows(data_dir / "listings.csv")[0]["priority_zone"] == "True"


# --- house numbers, on the way through --------------------------------------


def test_a_scrape_run_gives_a_new_listing_its_house_number(
        no_sleep, paths, tmp_path):
    """The wiring, not the matcher.

    fill_house_numbers can be perfectly correct and never called, and the
    whole feature would then do nothing between monthly index builds while
    every one of its own tests still passed. That is exactly what the first
    version did.
    """
    from tools import build_ruian_index

    data_dir, logs_dir = paths
    data_dir.mkdir(parents=True, exist_ok=True)

    # One address point, on the street and at the coordinate the fake
    # sreality listing reports.
    build_ruian_index.write_index([{
        "kod_adm": "12345678",
        "ulice": "nurniho",
        "ulice_original": "Nurniho",
        "cislo_domovni": "1481",
        "typ_cisla": "č.p.",
        "cislo_orientacni": "6",
        "znak_orientacniho": "",
        "mestska_cast": "Praha 4",
        "obvod": "Praha 4",
        "cast_obce": "Spořilov",
        "psc": "14100",
        "lat": f"{NURMIHO_LAT:.6f}",
        "lon": f"{NURMIHO_LON:.6f}",
    }], str(data_dir / "ruian_praha.csv.gz"))

    fake = FakeSreality(present=[900001])
    do_run(fake, data_dir, logs_dir)

    rows = read_csv_rows(data_dir / "listings.csv")
    assert rows, "the run collected nothing, so this proves nothing"
    row = rows[0]
    assert row["cislo_popisne"] == "1481", row
    assert row["cislo_orientacni"] == "6"
    assert row["cislo_typ"] == "č.p."
    assert row["cislo_zdroj"] == "ruian"
    assert float(row["cislo_vzdalenost_m"]) < 1.0


def test_a_scrape_run_without_an_index_still_collects(no_sleep, paths):
    """The index is built by a separate monthly workflow. A scrape that runs
    before the first build - or while that build is failing - must still
    write its listings."""
    data_dir, logs_dir = paths
    fake = FakeSreality(present=[900002])
    assert do_run(fake, data_dir, logs_dir) == 0

    rows = read_csv_rows(data_dir / "listings.csv")
    assert len(rows) == 1
    assert rows[0]["cislo_zdroj"] == ""
