"""The attribute-change log.

Price and status have had their own layer since the start. Everything else a
listing says about itself was simply overwritten, so a flat advertised as
2+kk and later as 3+1 left no trace of ever having said 2+kk. That is worth
knowing: an attribute correction is usually a re-listing dressed up as an
edit, or a seller repositioning, and a rewritten description very often
arrives alongside a price cut.

The failure mode this has to avoid is the opposite one - logging a change
every hour because a number came back from a CSV as a string.
"""

import csv
from datetime import datetime, timezone

import pytest

from common import storage
from common.schema import NormalizedListing
from run import merge_source

SCOPES = {("byt", "prodej")}


def listing(source_id="1", disposition="2+kk", area=55.0, floor=3,
            address="Roztylske namesti, Praha", description="Hezky byt",
            price=7_000_000):
    row = NormalizedListing(
        source="sreality", source_id=source_id,
        url=f"https://www.sreality.cz/detail/{source_id}",
        property_type="byt", transaction_type="prodej",
        disposition=disposition, area_m2=area, price=price,
        lat=50.0400, lon=14.4800, address=address, description=description,
    )
    row.floor = floor
    return row


def merge(normalized, listings, last_obs, when):
    return merge_source("sreality", normalized, [], listings, last_obs, when,
                        [], SCOPES)


def first_run():
    listings, last_obs = {}, {}
    _, _, changes = merge([listing()], listings, last_obs,
                          "2026-03-01T10:00:00+00:00")
    assert changes == [], "a first sighting is not a change of anything"
    return listings, last_obs


# --- what must be logged ----------------------------------------------------


@pytest.mark.parametrize("field,argument,value", [
    ("disposition", "disposition", "3+1"),
    ("area_m2", "area", 62.0),
    ("floor", "floor", 5),
    ("address", "address", "Jihovychodni IV, Praha"),
    ("description", "description", "Kompletne zrekonstruovany byt"),
])
def test_a_changed_attribute_is_logged(field, argument, value):
    listings, last_obs = first_run()
    _, _, changes = merge([listing(**{argument: value})], listings, last_obs,
                          "2026-03-02T10:00:00+00:00")

    logged = [c for c in changes if c["field"] == field]
    assert len(logged) == 1, f"{field} changed without being logged"
    assert logged[0]["new_value"] == str(value)
    assert logged[0]["old_value"] not in ("", str(value))
    assert logged[0]["internal_id"] in listings


def test_the_new_value_is_what_ends_up_in_listings():
    listings, last_obs = first_run()
    merge([listing(disposition="3+1")], listings, last_obs, "2026-03-02T10:00:00+00:00")
    assert list(listings.values())[0]["disposition"] == "3+1"


def test_several_fields_changing_at_once_are_several_rows():
    listings, last_obs = first_run()
    _, _, changes = merge([listing(disposition="3+1", area=62.0)],
                          listings, last_obs, "2026-03-02T10:00:00+00:00")
    assert {c["field"] for c in changes} == {"disposition", "area_m2"}


# --- what must NOT be logged ------------------------------------------------


def test_an_unchanged_listing_logs_nothing():
    """The whole thing has to be silent in the normal case, or an hourly run
    writes thousands of rows a day saying nothing happened."""
    listings, last_obs = first_run()
    _, _, changes = merge([listing()], listings, last_obs, "2026-03-02T10:00:00+00:00")
    assert changes == []


def test_a_number_that_went_through_a_csv_is_not_a_change():
    """55.0 comes back out of listings.csv as the string "55.0". Comparing it
    naively logs a change every hour, for every listing, for ever - which
    would bury the real edits completely."""
    listings, last_obs = first_run()
    row = list(listings.values())[0]
    row["area_m2"] = "55.0"      # as read back from CSV
    row["floor"] = "3"

    _, _, changes = merge([listing()], listings, last_obs, "2026-03-02T10:00:00+00:00")
    assert changes == [], f"a CSV round-trip was mistaken for an edit: {changes}"


def test_a_blank_reading_never_overwrites_or_logs():
    """A field that came back empty this run is a hiccup on that field, not
    the seller deleting it."""
    listings, last_obs = first_run()
    _, _, changes = merge([listing(description="", disposition="")],
                          listings, last_obs, "2026-03-02T10:00:00+00:00")
    assert changes == []
    assert list(listings.values())[0]["disposition"] == "2+kk"


def test_the_price_is_not_in_this_log():
    """It has its own layer, and duplicating it here would mean two sources of
    truth for the one field that matters most."""
    listings, last_obs = first_run()
    _, _, changes = merge([listing(price=6_500_000)], listings, last_obs,
                          "2026-03-02T10:00:00+00:00")
    assert [c["field"] for c in changes] == []


# --- the file it writes -----------------------------------------------------


def test_changes_are_appended_month_by_month(tmp_path):
    when = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
    storage.append_changes(
        [{"internal_id": "a", "changed_at": when.isoformat(), "field": "disposition",
          "old_value": "2+kk", "new_value": "3+1"}], when, tmp_path)
    storage.append_changes(
        [{"internal_id": "b", "changed_at": when.isoformat(), "field": "area_m2",
          "old_value": "55.0", "new_value": "62.0"}], when, tmp_path)

    path = tmp_path / "2026-03.csv"
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["internal_id"] for r in rows] == ["a", "b"], "the log is append-only"
    assert rows[0]["old_value"] == "2+kk"


def test_an_empty_batch_writes_no_file(tmp_path):
    when = datetime(2026, 3, 2, 10, tzinfo=timezone.utc)
    assert storage.append_changes([], when, tmp_path) == 0
    assert not (tmp_path / "2026-03.csv").exists()


def test_a_very_long_description_is_truncated_rather_than_stored_whole():
    """Two full descriptions per edit, in a file that never shrinks, is a
    repository-size problem for something whose value is knowing that it
    changed."""
    listings, last_obs = first_run()
    _, _, changes = merge([listing(description="x" * 5000)], listings, last_obs,
                          "2026-03-02T10:00:00+00:00")
    assert len(changes) == 1
    assert len(changes[0]["new_value"]) <= 300


# --- learning a field is not the seller editing it ---------------------------


def test_a_description_arriving_late_is_not_an_edit():
    """The detail-fetch budget means a description usually arrives a run or
    two after the listing. The second run of the first clean day logged 145
    of these against 2 real edits; counted as changes they would make
    "upravilo" a chart of our own fetch backlog."""
    listings, last_obs = {}, {}
    merge([listing(description=None)], listings, last_obs,
          "2026-03-01T10:00:00+00:00")

    _, _, changes = merge([listing(description="Hezky byt v cihle")],
                          listings, last_obs, "2026-03-01T11:00:00+00:00")
    assert changes == [], changes


def test_the_late_value_is_still_stored():
    """Not counting it as a change must not mean throwing it away."""
    listings, last_obs = {}, {}
    merge([listing(description=None)], listings, last_obs,
          "2026-03-01T10:00:00+00:00")
    merge([listing(description="Hezky byt v cihle")], listings, last_obs,
          "2026-03-01T11:00:00+00:00")
    row, = listings.values()
    assert row["description"] == "Hezky byt v cihle"


def test_a_rewrite_of_a_description_we_already_had_is_an_edit():
    """The exemption is for empty to something, not for every write."""
    listings, last_obs = first_run()
    _, _, changes = merge([listing(description="Po rekonstrukci, nova cena")],
                          listings, last_obs, "2026-03-01T11:00:00+00:00")
    assert [c["field"] for c in changes] == ["description"]
    assert changes[0]["old_value"] == "Hezky byt"
