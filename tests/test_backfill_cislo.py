"""Filling the house number across the whole history, and re-filling it."""

from __future__ import annotations

from collections import Counter

from common import ruian
from tests.test_ruian import STREET, at, index_of, point
from tools import backfill_cislo


def listing(internal_id: str, north_m=None, east_m=None, ulice="Leopoldova",
            **extra) -> dict:
    row = {"internal_id": internal_id, "ulice": ulice}
    if north_m is not None:
        lat, lon = at(north_m, east_m)
        row["lat"] = str(lat)
        row["lon"] = str(lon)
    else:
        row["lat"] = ""
        row["lon"] = ""
    row.update(ruian.blank_match())
    row.update(extra)
    return row


def test_a_matchable_row_is_filled():
    rows = {"a": listing("a", 0, 30)}
    changed, stats = backfill_cislo.backfill(rows, STREET)
    assert changed == 1
    assert rows["a"]["cislo_popisne"] == "2310"
    assert rows["a"]["cislo_zdroj"] == "ruian"
    assert stats["reasons"]["matched"] == 1


def test_running_twice_changes_nothing_the_second_time():
    rows = {"a": listing("a", 0, 30)}
    backfill_cislo.backfill(rows, STREET)
    changed, _ = backfill_cislo.backfill(rows, STREET)
    assert changed == 0


def test_a_row_that_stops_matching_is_cleared_not_left_stale():
    """A row that matched against a previous index and no longer does must
    lose the number. Left behind, it would be a house number attributed to a
    street the register no longer places there, with a cislo_zdroj still
    claiming the register as its source."""
    rows = {"a": listing("a", 0, 30)}
    backfill_cislo.backfill(rows, STREET)
    assert rows["a"]["cislo_popisne"] == "2310"

    empty = index_of(point("jinde", "1", 0, 0))
    changed, _ = backfill_cislo.backfill(rows, empty)
    assert changed == 1
    assert rows["a"]["cislo_popisne"] == ""
    assert rows["a"]["cislo_zdroj"] == ""
    assert rows["a"]["cislo_vzdalenost_m"] == ""


def test_the_reasons_tell_the_two_kinds_of_miss_apart():
    """One is fixable here and one is not: a street the register does not
    know is an abbreviation or a parser gap, while a pin 5 km away is the
    portal's and no amount of work on this side changes it."""
    rows = {
        "far": listing("far", 0, 5000),
        "unknown": listing("unknown", 0, 0, ulice="Neexistujici"),
        "nogps": listing("nogps"),
        "nostreet": listing("nostreet", 0, 0, ulice=""),
        "ok": listing("ok", 0, 30),
    }
    _, stats = backfill_cislo.backfill(rows, STREET)
    reasons = stats["reasons"]
    assert reasons["nearest point too far"] == 1
    assert reasons["street not in the register"] == 1
    assert reasons["no coordinates"] == 1
    assert reasons["no street"] == 1
    assert reasons["matched"] == 1


def test_a_row_with_only_one_coordinate_is_not_half_matched():
    rows = {"a": listing("a", 0, 30)}
    rows["a"]["lon"] = ""
    _, stats = backfill_cislo.backfill(rows, STREET)
    assert stats["reasons"]["no coordinates"] == 1


def test_an_unparseable_coordinate_is_a_miss_not_a_crash():
    rows = {"a": listing("a", 0, 30)}
    rows["a"]["lat"] = "nekde"
    _, stats = backfill_cislo.backfill(rows, STREET)
    assert stats["reasons"]["no coordinates"] == 1


def test_the_distance_distribution_is_collected():
    rows = {"near": listing("near", 0, 30), "far": listing("far", 40, 30)}
    _, stats = backfill_cislo.backfill(rows, STREET)
    assert len(stats["distances"]) == 2
    assert min(stats["distances"]) < 1
    assert 35 < max(stats["distances"]) < 45


def test_percentiles_of_a_known_spread():
    values = list(range(1, 101))
    assert backfill_cislo.percentile(values, 0.50) == 51
    assert backfill_cislo.percentile(values, 0.10) == 11
    assert backfill_cislo.percentile(values, 0.99) == 100
    assert backfill_cislo.percentile(values, 1.0) == 100


def test_percentiles_of_nothing_do_not_raise():
    import math
    assert math.isnan(backfill_cislo.percentile([], 0.5))


def test_the_report_survives_a_run_that_matched_nothing(capsys):
    rows = {"a": listing("a", 0, 0, ulice="Neexistujici")}
    _, stats = backfill_cislo.backfill(rows, STREET)
    backfill_cislo.report(len(rows), stats)
    assert "no distribution" in capsys.readouterr().out


def test_the_report_states_the_share_within_one_building(capsys):
    rows = {"near": listing("near", 0, 30), "far": listing("far", 200, 30)}
    _, stats = backfill_cislo.backfill(rows, STREET)
    backfill_cislo.report(len(rows), stats)
    out = capsys.readouterr().out
    assert "within 25 m" in out
    assert "1 of 2" in out
