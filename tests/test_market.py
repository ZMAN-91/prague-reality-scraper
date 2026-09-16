"""The ten indicators, tested on markets whose answer is known by hand.

Each fixture is a small market built to produce one obvious number, so a
regression shows up as a wrong answer rather than as a plausible one.
"""

import csv

import pytest

from tools import market


def episode(key, first, last, outcome="removed", price=7_000_000,
            first_price=None, days=None, changes=0, discount=0,
            ptype="byt", transaction="prodej", pm2=130_000):
    from datetime import date
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return {
        "property_key": key, "episode": 1,
        "property_type": ptype, "transaction_type": transaction,
        "first_seen": first, "last_seen": last,
        "days_on_market": days if days is not None else (end - start).days,
        "outcome": outcome,
        "first_price": first_price if first_price is not None else price,
        "last_price": price,
        "price_changes": changes,
        "discount_czk": discount,
        "discount_pct": round(100 * discount / (first_price or price), 2) if discount else "",
        "price_per_m2_last": pm2,
    }


def day(series, when, segment="vse"):
    rows = [r for r in series if r["den"] == when and r["segment"] == segment]
    assert len(rows) == 1, f"expected one row for {when}/{segment}, got {len(rows)}"
    return rows[0]


def market_of(n, **kwargs):
    """n identical properties, enough to clear the small-sample floor."""
    return [episode(f"p{i}", **kwargs) for i in range(n)]


# --- 1-3: supply, arrivals, departures --------------------------------------


def test_supply_counts_what_is_on_the_market_that_day():
    series = market.daily(market_of(10, first="2026-01-01", last="2026-01-10"))
    assert day(series, "2026-01-05")["nabidka"] == 10
    assert day(series, "2026-01-01")["nove"] == 10
    assert day(series, "2026-01-10")["zmizele"] == 10


def test_a_property_still_on_the_market_never_counts_as_a_departure():
    series = market.daily(market_of(10, first="2026-01-01", last="2026-01-10",
                                    outcome="active"))
    assert all(r["zmizele"] == 0 for r in series)


def test_supply_drops_the_day_after_a_departure():
    """A property that left on the 5th was still on the market on the 5th -
    it is counted that day and gone the next. One survivor keeps the series
    running past the departure, which otherwise ends with the data."""
    rows = (market_of(10, first="2026-01-01", last="2026-01-05")
            + [episode("stays", "2026-01-01", "2026-01-09", outcome="active")])
    series = market.daily(rows)
    assert day(series, "2026-01-05")["nabidka"] == 11, "it was on the market that day"
    assert day(series, "2026-01-06")["nabidka"] == 1


# --- 4: absorption ----------------------------------------------------------


def test_absorption_is_departures_over_supply():
    rows = (market_of(8, first="2026-01-01", last="2026-01-20", outcome="active")
            + [episode(f"gone{i}", "2026-01-01", "2026-01-10") for i in range(2)])
    series = market.daily(rows)
    assert day(series, "2026-01-10")["absorpce_pct"] == 20.0


# --- 5-6: price levels ------------------------------------------------------


def test_the_median_is_used_so_one_villa_cannot_move_it():
    rows = market_of(9, first="2026-01-01", last="2026-01-20", outcome="active")
    rows.append(episode("villa", "2026-01-01", "2026-01-20", outcome="active",
                        price=90_000_000))
    row = day(market.daily(rows), "2026-01-10")
    assert row["cena_median"] == 7_000_000, "a mean would read about 15M"


def test_the_price_used_is_the_one_being_asked_now():
    """An episode that cut its price twice is asking the third figure; the
    first two are history, not the state of the market."""
    rows = market_of(6, first="2026-01-01", last="2026-01-20", outcome="active",
                     first_price=8_000_000, price=7_000_000, discount=1_000_000)
    assert day(market.daily(rows), "2026-01-10")["cena_median"] == 7_000_000


# --- 7: what left, and what left quickly ------------------------------------


def test_quick_departures_are_measured_separately():
    """The gap between these two is the read on what the market will pay
    versus what it is being asked."""
    rows = ([episode(f"q{i}", "2026-01-01", "2026-01-08", price=6_000_000)
             for i in range(5)]
            + [episode(f"s{i}", "2025-10-01", "2026-01-08", price=9_000_000)
               for i in range(5)])
    row = day(market.daily(rows), "2026-01-08")
    assert row["cena_zmizelych"] == 7_500_000, "all ten, median across both groups"
    assert row["cena_rychlych"] == 6_000_000, "only those gone within a fortnight"


def test_the_quick_threshold_is_a_fortnight():
    rows = [episode(f"p{i}", "2026-01-01", "2026-01-16") for i in range(6)]
    assert day(market.daily(rows), "2026-01-16")["cena_rychlych"] is None


# --- 8: time on market ------------------------------------------------------


def test_days_on_market_is_measured_over_what_left():
    rows = [episode(f"p{i}", "2026-01-01", "2026-01-11") for i in range(6)]
    assert day(market.daily(rows), "2026-01-11")["dnu_na_trhu_median"] == 10


# --- 9-10: discounting ------------------------------------------------------


def test_the_share_that_discounted_and_how_often():
    rows = (market_of(6, first="2026-01-01", last="2026-01-20", outcome="active",
                      first_price=8_000_000, price=7_200_000,
                      discount=800_000, changes=2)
            + [episode(f"firm{i}", "2026-01-01", "2026-01-20", outcome="active")
               for i in range(6)])
    row = day(market.daily(rows), "2026-01-10")
    assert row["zlevnilo_pct"] == 50.0
    assert row["zlevneni_prumer"] == 1.0, "two cuts among half of them"
    assert row["sleva_median_pct"] == 10.0


def test_a_price_rise_is_not_a_discount():
    rows = market_of(6, first="2026-01-01", last="2026-01-20", outcome="active",
                     first_price=7_000_000, price=7_500_000, discount=-500_000)
    assert day(market.daily(rows), "2026-01-10")["zlevnilo_pct"] == 0.0


# --- the small-sample floor -------------------------------------------------


def test_a_thin_day_reports_nothing_rather_than_noise():
    """Two properties have a median. It is not a measurement of a market, and
    reporting it as one turns every quiet Tuesday into a market swing."""
    rows = market_of(2, first="2026-01-01", last="2026-01-10")
    row = day(market.daily(rows), "2026-01-05")
    assert row["nabidka"] == 2, "the count is still a fact"
    assert row["cena_median"] is None
    assert row["zlevnilo_pct"] is None


# --- segmentation -----------------------------------------------------------


def test_sale_and_rent_are_never_averaged_together():
    rows = (market_of(6, first="2026-01-01", last="2026-01-20", outcome="active")
            + [episode(f"r{i}", "2026-01-01", "2026-01-20", outcome="active",
                       transaction="pronajem", price=25_000) for i in range(6)])
    series = market.daily(rows)
    assert day(series, "2026-01-10", "byt/prodej")["cena_median"] == 7_000_000
    assert day(series, "2026-01-10", "byt/pronajem")["cena_median"] == 25_000


def test_the_combined_row_exists_alongside_the_segments():
    rows = market_of(6, first="2026-01-01", last="2026-01-20", outcome="active")
    series = market.daily(rows)
    assert day(series, "2026-01-10", "vse")["nabidka"] == 6
    assert day(series, "2026-01-10", "byt/prodej")["nabidka"] == 6


# --- the trend summary ------------------------------------------------------


def test_the_summary_compares_now_against_a_month_ago():
    rows = ([episode(f"old{i}", "2026-01-01", "2026-03-01", outcome="active",
                     price=8_000_000) for i in range(6)]
            + [episode(f"new{i}", "2026-02-15", "2026-03-01", outcome="active",
                       price=8_000_000) for i in range(6)])
    series = market.daily(rows)
    summary = market.summary(series, window=30)
    supply = [r for r in summary
              if r["segment"] == "vse" and r["ukazatel"] == "nabidka"][0]
    assert supply["ted"] == 12 and supply["tehdy"] == 6
    assert supply["zmena_pct"] == 100.0


def test_the_summary_survives_an_indicator_that_was_not_measurable():
    rows = market_of(6, first="2026-01-01", last="2026-03-01", outcome="active")
    summary = market.summary(market.daily(rows), window=30)
    assert summary, "a summary must still be produced"
    assert all("zmena_pct" in r for r in summary)


# --- the files --------------------------------------------------------------


def test_export_writes_both_files(tmp_path):
    (tmp_path / "csv").mkdir(parents=True)
    rows = market_of(6, first="2026-01-01", last="2026-01-20", outcome="active")
    with open(tmp_path / "csv" / "historie_nemovitosti.csv", "w",
              encoding="utf-8", newline="") as f:
        from tools.episodes import FIELDS as EP_FIELDS
        writer = csv.DictWriter(f, fieldnames=EP_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    stats = market.export(tmp_path)
    assert stats["days"] == 20

    with open(stats["daily"], encoding="utf-8", newline="") as f:
        daily_rows = list(csv.DictReader(f))
    assert list(daily_rows[0]) == market.FIELDS
    assert daily_rows[0]["nabidka"] == "6"


# --- 11-12: edits other than the price --------------------------------------


def test_the_share_that_edited_something_other_than_the_price():
    """A rewritten description is very often the move before a price cut, and
    a corrected disposition or area is usually a re-listing dressed up as an
    edit. This indicator turns before the discounting one does."""
    rows = []
    for i in range(6):
        row = episode(f"edited{i}", "2026-01-01", "2026-01-20", outcome="active")
        row["attribute_changes"] = 2
        rows.append(row)
    for i in range(6):
        row = episode(f"quiet{i}", "2026-01-01", "2026-01-20", outcome="active")
        row["attribute_changes"] = 0
        rows.append(row)

    row = day(market.daily(rows), "2026-01-10")
    assert row["upravilo_pct"] == 50.0
    assert row["uprav_prumer"] == 1.0, "two edits among half of them"


def test_a_property_that_never_edited_anything_is_not_counted():
    rows = market_of(6, first="2026-01-01", last="2026-01-20", outcome="active")
    for r in rows:
        r["attribute_changes"] = 0
    assert day(market.daily(rows), "2026-01-10")["upravilo_pct"] == 0.0
