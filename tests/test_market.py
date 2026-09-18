"""The indicators, on markets whose answer is known by hand.

Every fixture is built to produce one obvious number, so a regression shows up
as a wrong answer rather than as a plausible one. Several tests are the first
version's defects written down, so they cannot come back.
"""

import csv
from datetime import date

import pytest

from tools import market


def episode(key, first, last, outcome="removed", price=7_000_000,
            days=None, cuts=(), edits=(), discount_pct="",
            ptype="byt", transaction="prodej", pm2=130_000):
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    return {
        "property_key": key, "episode": 1,
        "property_type": ptype, "transaction_type": transaction,
        "first_seen": first, "last_seen": last,
        "days_on_market": days if days is not None else (end - start).days,
        "outcome": outcome,
        "first_price": price, "last_price": price,
        "price_per_m2_last": pm2,
        "discount_pct": discount_pct,
        "cut_days": "|".join(cuts),
        "edit_days": "|".join(edits),
    }


def cell(series, when, window=1, segment="vse/prodej"):
    rows = [r for r in series
            if r["den"] == when and r["okno_dnu"] == window
            and r["segment"] == segment]
    assert len(rows) == 1, f"expected one row for {when}/{window}d/{segment}"
    return rows[0]


def market_of(n, **kwargs):
    return [episode(f"p{i}", **kwargs) for i in range(n)]


LONG = dict(first="2026-01-01", last="2026-03-31", outcome="active")


# --- level: what is on the market -------------------------------------------


def test_supply_counts_what_is_on_the_market_that_day():
    series = market.daily(market_of(10, **LONG))
    assert cell(series, "2026-02-01")["nabidka"] == 10


def test_both_a_median_and_a_mean_are_reported():
    """The gap between them is itself informative: a mean far above the median
    means the top end is doing the talking."""
    rows = market_of(9, **LONG)
    rows.append(episode("villa", price=90_000_000, **LONG))
    row = cell(market.daily(rows), "2026-02-01")
    assert row["cena_median"] == 7_000_000
    assert row["cena_prumer"] == 15_300_000.0


def test_the_age_of_the_current_stock_is_reported():
    series = market.daily(market_of(6, **LONG))
    assert cell(series, "2026-01-31")["stari_median_dnu"] == 30


# --- flow: windows, not single days -----------------------------------------


def test_arrivals_are_counted_over_the_window():
    """Six on one day is noise; six over thirty days is a rate."""
    rows = [episode(f"p{i}", first=f"2026-02-{i+1:02d}", last="2026-03-31",
                    outcome="active") for i in range(10)]
    series = market.daily(rows)
    assert cell(series, "2026-02-10", window=1)["nove"] == 1
    assert cell(series, "2026-02-10", window=7)["nove"] == 7
    assert cell(series, "2026-02-10", window=30)["nove"] == 10


def test_arrivals_per_day_makes_windows_comparable():
    """Once the dataset reaches back further than the window, the divisor is
    the window: ten arrivals over thirty days is a third of one a day."""
    rows = ([episode("anchor", first="2025-12-01", last="2026-03-31",
                     outcome="active")]
            + [episode(f"p{i}", first=f"2026-02-{i+1:02d}", last="2026-03-31",
                       outcome="active") for i in range(10)])
    assert cell(market.daily(rows), "2026-02-10", window=30)["nove_denne"] == \
        round(10 / 30, 2)


def test_departures_are_counted_over_the_window():
    rows = (market_of(20, **LONG)
            + [episode(f"g{i}", first="2026-01-01", last=f"2026-02-{i+1:02d}")
               for i in range(10)])
    series = market.daily(rows)
    assert cell(series, "2026-02-10", window=7)["zmizele"] == 7
    assert cell(series, "2026-02-10", window=30)["zmizele"] == 10


def test_the_rate_denominator_is_mean_supply_not_one_days_stock():
    """Dividing a month of departures by a single day's stock overstates the
    rate by however much the stock moved across the month."""
    rows = ([episode(f"old{i}", first="2026-01-01", last="2026-03-31",
                     outcome="active") for i in range(10)]
            + [episode(f"new{i}", first="2026-02-25", last="2026-03-31",
                       outcome="active") for i in range(90)])
    row = cell(market.daily(rows), "2026-02-28", window=30)
    assert row["nabidka"] == 100
    assert row["nabidka_prumer"] < 60, "mean supply must reflect the whole window"


def test_months_of_inventory_answers_the_sellers_market_question():
    """Ten on the market, five leaving a month: two months to clear."""
    rows = (market_of(10, first="2026-01-01", last="2026-03-31", outcome="active")
            + [episode(f"g{i}", first="2026-01-01", last="2026-02-28")
               for i in range(5)])
    row = cell(market.daily(rows), "2026-02-28", window=30)
    assert row["mesicu_zasoby"] == pytest.approx(3.0, abs=0.6)


# --- the defects of the first version ---------------------------------------


def test_the_average_number_of_cuts_is_taken_among_those_that_cut():
    """It read 0.00 when the answer was 1.08, because it averaged over the
    whole stock. A metric that reads zero when the answer is one is worse
    than no metric."""
    rows = (market_of(97, **LONG)
            + [episode(f"c{i}", cuts=("2026-02-10",), discount_pct=8.0, **LONG)
               for i in range(3)])
    row = cell(market.daily(rows), "2026-02-10", window=30)
    assert row["zlevnilo"] == 3
    assert row["zlevneni_prumer"] == 1.0, "averaged over the stock this is 0.03"


def test_discounting_is_counted_in_the_window_not_for_ever():
    """"How many discounted in the last 30 days" is the question worth asking;
    "how many have ever discounted" only ever goes up."""
    rows = market_of(20, cuts=("2026-01-05",), discount_pct=8.0, **LONG)
    series = market.daily(rows)
    assert cell(series, "2026-01-10", window=30)["zlevnilo"] == 20
    assert cell(series, "2026-03-01", window=30)["zlevnilo"] == 0, \
        "a cut in January is not a cut in the last thirty days of March"


def test_two_cuts_by_one_property_count_once_as_a_property():
    rows = market_of(20, cuts=("2026-02-05", "2026-02-20"),
                     discount_pct=12.0, **LONG)
    row = cell(market.daily(rows), "2026-02-28", window=30)
    assert row["zlevnilo"] == 20, "twenty properties, not forty cuts"
    assert row["zlevneni_prumer"] == 2.0


def test_edits_are_windowed_the_same_way():
    rows = market_of(20, edits=("2026-02-05",), **LONG)
    series = market.daily(rows)
    assert cell(series, "2026-02-10", window=30)["upravilo"] == 20
    assert cell(series, "2026-03-31", window=30)["upravilo"] == 0


# --- the two artefacts that can only be disclosed ----------------------------


def test_how_much_history_a_figure_had_is_reported():
    """On day two, "median days on market" was 1 - not because Prague sells
    flats in a day but because that is as far back as the record goes."""
    rows = market_of(10, first="2026-01-01", last="2026-01-02", outcome="active")
    assert cell(market.daily(rows), "2026-01-01")["uplnost_dnu"] == 1
    assert cell(market.daily(rows), "2026-01-02")["uplnost_dnu"] == 2


def test_the_unconfirmed_tail_of_departures_is_flagged():
    """A departure is only confirmed after a week of absence, so the last
    seven days always under-count."""
    rows = market_of(10, first="2026-01-01", last="2026-03-31", outcome="active")
    series = market.daily(rows)
    assert cell(series, "2026-03-31", window=1)["zmizele_potvrzeno"] == "ne"
    assert cell(series, "2026-03-31", window=30)["zmizele_potvrzeno"] == "ano"


# --- what left, and what left quickly ---------------------------------------


def test_quick_departures_are_measured_separately():
    rows = ([episode(f"q{i}", first="2026-02-20", last="2026-02-28",
                     price=6_000_000) for i in range(5)]
            + [episode(f"s{i}", first="2026-01-01", last="2026-02-28",
                       price=9_000_000) for i in range(5)])
    row = cell(market.daily(rows), "2026-02-28", window=30)
    assert row["cena_zmizelych_median"] == 7_500_000
    assert row["cena_rychlych_median"] == 6_000_000


def test_a_departure_after_a_fortnight_is_not_quick():
    rows = [episode(f"p{i}", first="2026-01-01", last="2026-02-28")
            for i in range(6)]
    assert cell(market.daily(rows), "2026-02-28",
                window=30)["cena_rychlych_median"] is None


# --- segmentation ------------------------------------------------------------


def test_sale_and_rent_are_never_averaged_together():
    rows = (market_of(6, **LONG)
            + [episode(f"r{i}", transaction="pronajem", price=25_000, **LONG)
               for i in range(6)])
    series = market.daily(rows)
    assert cell(series, "2026-02-01", segment="byt/prodej")["cena_median"] == 7_000_000
    assert cell(series, "2026-02-01", segment="byt/pronajem")["cena_median"] == 25_000
    assert "vse" not in {r["segment"] for r in series}


def test_the_roll_up_spans_property_types_within_one_transaction():
    rows = (market_of(6, **LONG)
            + [episode(f"h{i}", ptype="dum", price=20_000_000, **LONG)
               for i in range(6)])
    series = market.daily(rows)
    assert cell(series, "2026-02-01", segment="vse/prodej")["nabidka"] == 12
    assert cell(series, "2026-02-01", segment="byt/prodej")["nabidka"] == 6


# --- the small-sample floor --------------------------------------------------


def test_a_thin_market_reports_counts_but_not_medians():
    """Two properties have a median. It is not a measurement of a market, and
    reporting it as one turns every quiet Tuesday into a market swing."""
    row = cell(market.daily(market_of(2, **LONG)), "2026-02-01")
    assert row["nabidka"] == 2
    assert row["cena_median"] is None and row["cena_prumer"] is None


# --- the files ---------------------------------------------------------------


def test_export_writes_both_files(tmp_path):
    (tmp_path / "csv").mkdir(parents=True)
    from tools.episodes import FIELDS as EP_FIELDS
    with open(tmp_path / "csv" / "historie_nemovitosti.csv", "w",
              encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=EP_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(market_of(6, **LONG))

    stats = market.export(tmp_path)
    with open(stats["daily"], encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0]) == market.FIELDS
    assert {int(r["okno_dnu"]) for r in rows} == set(market.WINDOWS)


def test_the_series_is_recomputed_from_scratch_every_time(tmp_path):
    """It is derived, not accumulated. An incremental file drifts once and is
    wrong for ever; this one cannot be, and back-fills history the moment a
    metric is added."""
    rows = market_of(6, **LONG)
    first = market.daily(rows)
    assert market.daily(rows) == first


# --- the shape of the computation -------------------------------------------


def test_the_series_does_not_get_quadratically_slower():
    """Measured, not asserted by inspection: the straightforward version
    re-scanned every episode for every day of every window. That took 48.7
    seconds for 30 000 episodes over 180 days and extrapolated to a quarter
    of an hour for a year at full size - the whole hourly budget spent on
    arithmetic.

    Doubling the episodes must roughly double the time, not quadruple it.
    The bound is deliberately loose: this is here to catch a return to
    quadratic, not to police a few per cent.
    """
    import time
    from datetime import timedelta

    def synthetic(count, span_days):
        start = date(2026, 1, 1)
        rows = []
        for i in range(count):
            begins = start + timedelta(days=i % span_days)
            ends = min(begins + timedelta(days=45),
                       start + timedelta(days=span_days - 1))
            rows.append(episode(f"p{i}", begins.isoformat(), ends.isoformat(),
                                outcome="removed" if i % 3 else "active"))
        return rows

    def seconds(count):
        rows = synthetic(count, 120)
        started = time.perf_counter()
        market.daily(rows)
        return time.perf_counter() - started

    small = seconds(4_000)
    large = seconds(8_000)
    assert large < small * 3.0, (
        f"twice the episodes took {large / max(small, 1e-6):.1f}x the time - "
        "the day loop is scanning everything again"
    )


def test_a_rate_is_per_day_of_data_not_per_nominal_day():
    """On the first day the 30-day window covers 29 days that do not exist.
    Dividing by them reported 3354 arrivals as "111.80 per day" - a number
    about nothing. Seen in the first clean test run."""
    rows = market_of(100, first="2026-01-01", last="2026-03-31", outcome="active")
    series = market.daily(rows)

    first = cell(series, "2026-01-01", window=30)
    assert first["nove"] == 100
    assert first["nove_denne"] == 100.0, "one day of data, one day of divisor"

    # A month in, the window is real and the divisor is the window.
    later = cell(series, "2026-02-01", window=30)
    assert later["nove_denne"] == 0.0, "nothing arrived in that window"


def test_the_absorption_rate_uses_the_same_honest_denominator():
    rows = (market_of(30, first="2026-01-01", last="2026-03-31", outcome="active")
            + [episode(f"g{i}", first="2026-01-01", last="2026-01-01")
               for i in range(3)])
    row = cell(market.daily(rows), "2026-01-01", window=30)
    # Three of thirty-three left on the only day there is: that is 3 a day,
    # 90 a month, not 3 a month.
    assert row["absorpce_pct"] == pytest.approx(272.7, abs=1.0)


# --- the day in progress is marked as such ----------------------------------


def test_the_current_day_is_marked_incomplete():
    """Supply for today is an undercount until the day's sweeps have landed,
    and nothing in the numbers says so. The row does."""
    rows = market_of(5, first="2026-01-01", last="2026-02-10", outcome="active")
    series = market.daily(rows, today=date(2026, 2, 10))
    assert cell(series, "2026-02-10")["den_uplny"] == "ne"
    assert cell(series, "2026-02-09")["den_uplny"] == "ano"


def test_every_earlier_day_is_complete():
    rows = market_of(5, first="2026-01-01", last="2026-02-10", outcome="active")
    series = market.daily(rows, today=date(2026, 2, 10))
    marks = {r["den"]: r["den_uplny"] for r in series if r["okno_dnu"] == 1}
    assert set(marks.values()) == {"ano", "ne"}
    assert [d for d, m in marks.items() if m == "ne"] == ["2026-02-10"]
