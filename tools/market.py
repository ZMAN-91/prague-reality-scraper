"""Market indicators, measured over windows rather than over single days.

    python -m tools.market      -> data/csv/trh_denne.csv
                                   data/csv/trh_souhrn.csv

WHY THIS WAS REWRITTEN

The first version computed everything for one day and compared it against one
day a week or a month earlier. Against live data that produced numbers that
looked like measurements and were not:

  * "Zmizelé za den: 6" in a market of 2648. Six is noise. Comparing six
    against whatever the number happened to be exactly thirty days ago is
    comparing one coin toss to another.
  * "Průměrný počet zlevnění: 0.00", because the average was taken over the
    whole stock rather than over the properties that had actually cut. The
    true figure was 1.08. A metric that reads zero when the answer is one is
    worse than no metric.
  * Departures included every listing in missing_1 or missing_2 - 51 of
    10 990 - so a portal hiccup read as a wave of departures followed by a
    wave of arrivals when they came back. (Fixed in tools/episodes.py.)

So: every flow is now counted over a window (1, 7 or 30 days), every average
is taken over the population the question is about, and the two measurement
artefacts that cannot be fixed - only disclosed - are flagged in the data
rather than left for the reader to fall into.

THE TWO ARTEFACTS

*Left censoring.* Nothing can be older than the dataset. On day two, "median
days on market" was 1 - not because Prague sells flats in a day but because
that is as far back as the record goes. Duration metrics stay wrong for as
long as the typical listing lives, which here is months. `uplnost_dnu` says
how many days of history a figure had to work with; until it passes the
number being reported, the number is a floor, not a measurement.

*Right censoring.* A departure is only confirmed after a week of absence, so
the last seven days always under-count departures - the listings that left
yesterday are still sitting in missing_1. `zmizele_potvrzeno` marks the days
where that has settled. Everything after it is provisional and rising.

THE INDICATORS

Level, as of the day:
    nabidka                 properties on the market
    cena_median/_prumer     asking price; both, because the gap between them
                            is itself informative - a mean far above the
                            median means the top end is doing the talking
    cena_m2_median/_prumer  the same per square metre, the only way to
                            compare across a changing mix of sizes
    stari_median_dnu        how long the current stock has been sitting

Flow, over the window:
    nove / zmizele          arrivals and departures in the window
    nove_denne              arrivals per day, which is the comparable version
    nabidka_prumer          mean supply across the window
    absorpce_pct            departures / mean supply, per 30 days
    mesicu_zasoby           months of inventory: how long the market would
                            take to clear at this rate. The one number that
                            answers "is it a sellers' market" on its own
    cena_zmizelych_median   what left
    cena_rychlych_median    what left within a fortnight - the closest thing
                            to a sold price this data can offer
    dnu_na_trhu_median      how long departures had been listed
    zlevnilo                properties that cut their price IN the window
    zlevnilo_pct            as a share of mean supply
    zlevneni_prumer         cuts per discounting property - among those that
                            cut, not among everything
    sleva_median_pct        how deep those cuts went
    upravilo                properties that edited something other than price
    upravilo_pct            as a share of mean supply

HOW TO READ THEM

Supply up, absorption down, discounting up is a market turning against
sellers, and they move in that order. Months of inventory is the summary of
the first two. Editing turns before discounting, which turns before price.
The median asking price is the slowest of all and the most contaminated by
what happens to be for sale that month, so it confirms rather than warns.

WHAT NONE OF IT CAN SAY

Whether anything sold. No portal publishes it.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from common import cas, storage
from tools import episodes

# 1 is "today", 7 and 30 are what the report shows. A window of one day is
# kept because it is the only one that can say "right now", not because it
# measures anything on its own.
WINDOWS = (1, 7, 30)

# A departure inside a fortnight is the strongest evidence available that
# something sold rather than being withdrawn: a seller who gives up rarely
# does it in two weeks.
QUICK_DAYS = 14

# A departure takes this long to be confirmed (common/schema REMOVAL_AFTER_DAYS).
# Kept as its own name because what it means here is different: it is the
# length of the tail in which departure counts are still rising.
CONFIRMATION_LAG_DAYS = 7

# Below this many properties a median is noise, not a measurement.
MIN_SAMPLE = 5

FIELDS = [
    "den", "segment", "okno_dnu",
    "uplnost_dnu", "zmizele_potvrzeno", "den_uplny",
    "nabidka", "nabidka_prumer",
    "cena_median", "cena_prumer", "cena_m2_median", "cena_m2_prumer",
    "stari_median_dnu",
    "nove", "nove_denne", "zmizele", "zmizele_denne",
    "absorpce_pct", "mesicu_zasoby",
    "cena_zmizelych_median", "cena_rychlych_median",
    "dnu_na_trhu_median", "dnu_na_trhu_prumer",
    "zlevnilo", "zlevnilo_pct", "zlevneni_prumer",
    "sleva_median_pct", "sleva_prumer_pct",
    "upravilo", "upravilo_pct",
]


def as_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return cas.date_of(value)
    except ValueError:
        return None


def as_num(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def days_in(value: str) -> list[date]:
    """The pipe-separated day list episodes emits for cuts and edits."""
    out = []
    for part in (value or "").split("|"):
        day = as_date(part)
        if day:
            out.append(day)
    return out


def median(values: Iterable) -> Optional[float]:
    kept = [v for v in values if v is not None]
    if len(kept) < MIN_SAMPLE:
        # Empty rather than a number: a thin day must read as "not measured"
        # instead of as a swing in the market.
        return None
    return round(statistics.median(kept), 2)


def mean(values: Iterable) -> Optional[float]:
    kept = [v for v in values if v is not None]
    if len(kept) < MIN_SAMPLE:
        return None
    return round(sum(kept) / len(kept), 2)


def share(part: float, whole: float) -> Optional[float]:
    if not whole or whole < MIN_SAMPLE:
        return None
    return round(100.0 * part / whole, 2)


def segment_of(row: dict) -> str:
    return f"{row.get('property_type') or '?'}/{row.get('transaction_type') or '?'}"


def price_at(row: dict) -> Optional[float]:
    """What it is asking now. An episode that cut twice is asking the third
    figure; the first two are history, not the state of the market."""
    return as_num(row.get("last_price")) or as_num(row.get("first_price"))


def prepare(rows: list[dict]) -> list[dict]:
    """Parse once. Every window over every day re-reads these, and string
    parsing in that loop is the difference between a second and a minute."""
    out = []
    for row in rows:
        start, end = as_date(row.get("first_seen")), as_date(row.get("last_seen"))
        if not start or not end or end < start:
            continue
        out.append({
            "segment": segment_of(row),
            "start": start,
            "end": end,
            "gone": row.get("outcome") == "removed",
            "price": price_at(row),
            "pm2": as_num(row.get("price_per_m2_last")),
            "days": as_num(row.get("days_on_market")),
            "discount_pct": as_num(row.get("discount_pct")),
            "cuts": days_in(row.get("cut_days")),
            "edits": days_in(row.get("edit_days")),
        })
    return out


def levels_on(day: date, on_market: list[dict]) -> dict:
    """The indicators that describe the stock rather than the flow.

    Hoisted out of measure() because they do not depend on the window, and a
    median over five thousand prices computed once per window instead of once
    per day is three times the work for one answer.
    """
    return {
        "nabidka": len(on_market),
        "cena_median": median(r["price"] for r in on_market),
        "cena_prumer": mean(r["price"] for r in on_market),
        "cena_m2_median": median(r["pm2"] for r in on_market),
        "cena_m2_prumer": mean(r["pm2"] for r in on_market),
        "stari_median_dnu": median((day - r["start"]).days for r in on_market),
    }


def measure(day: date, window: int, segment: str, first_day: date,
            on_market: list[dict], arrived: list[dict], left: list[dict],
            supply_mean: float, levels: dict, covered: int,
            complete_through: date) -> dict:
    """Every indicator for one (day, window, segment).

    The caller does the slicing. It used to be done here, by scanning every
    episode for every day of every window - which measured 48.7 seconds for
    30 000 episodes over 180 days and extrapolated to roughly 18 minutes for
    a year at full size, i.e. the whole hourly budget spent on arithmetic.
    """
    since = day - timedelta(days=window - 1)

    quick = [r for r in left if (r["days"] or 0) <= QUICK_DAYS]

    cut_in_window = [r for r in on_market
                     if any(since <= d <= day for d in r["cuts"])]
    cuts_each = [sum(1 for d in r["cuts"] if since <= d <= day)
                 for r in cut_in_window]
    edited_in_window = [r for r in on_market
                        if any(since <= d <= day for d in r["edits"])]

    # Every rate is per DAY OF DATA, not per nominal day of the window. On
    # the first day the 30-day window covers 29 days that do not exist, and
    # dividing by them reported 3354 arrivals as "111.80 per day" - a number
    # about nothing. `covered` is how much of the window the dataset actually
    # reaches, which is the only denominator that is true on day one and
    # still true in a year.
    per_30 = len(left) * (30.0 / covered)

    return {
        "den": day.isoformat(),
        "segment": segment,
        "okno_dnu": window,
        # How much history this figure had. Until it passes the number being
        # reported, a duration is a floor rather than a measurement.
        "uplnost_dnu": (day - first_day).days + 1,
        # Departures are only confirmed after a week of absence, so the tail
        # of the series always under-counts them.
        "zmizele_potvrzeno": "ano" if (day - timedelta(days=CONFIRMATION_LAG_DAYS))
                             >= since else "ne",
        # Whether the day was over when this was built. An episode leaves the
        # live set on its last_seen, so one not yet swept today looks like it
        # ended yesterday, and today's supply is an undercount until the day's
        # sweeps have all landed.
        "den_uplny": "ano" if day <= complete_through else "ne",

        "nabidka_prumer": round(supply_mean, 2),
        **levels,

        "nove": len(arrived),
        "nove_denne": round(len(arrived) / covered, 2),
        "zmizele": len(left),
        "zmizele_denne": round(len(left) / covered, 2),
        "absorpce_pct": share(per_30, supply_mean),
        # Months of inventory: at this rate of departures, how long the
        # current stock would take to clear. The one number that answers
        # "is this a sellers' market" without a second number beside it.
        "mesicu_zasoby": (round(supply_mean / per_30, 2)
                          if per_30 and supply_mean >= MIN_SAMPLE else None),

        "cena_zmizelych_median": median(r["price"] for r in left),
        "cena_rychlych_median": median(r["price"] for r in quick),
        "dnu_na_trhu_median": median(r["days"] for r in left),
        "dnu_na_trhu_prumer": mean(r["days"] for r in left),

        "zlevnilo": len(cut_in_window),
        "zlevnilo_pct": share(len(cut_in_window), supply_mean),
        # Among those that cut. Averaged over the whole stock this read 0.00
        # when the answer was 1.08.
        "zlevneni_prumer": (round(sum(cuts_each) / len(cuts_each), 2)
                            if cuts_each else None),
        "sleva_median_pct": median(r["discount_pct"] for r in cut_in_window),
        "sleva_prumer_pct": mean(r["discount_pct"] for r in cut_in_window),

        "upravilo": len(edited_in_window),
        "upravilo_pct": share(len(edited_in_window), supply_mean),
    }


def daily(rows: list[dict], today: Optional[date] = None,
          windows=WINDOWS) -> list[dict]:
    """One row per (day, segment, window), over the whole span of the data.

    Built from episodes, so a property advertised by three agencies counts
    once and one re-listed three times counts as one continuous episode. Both,
    left alone, inflate supply and shorten time on market.

    Everything is bucketed by day once, and the live set is carried forward
    day to day rather than rebuilt: the straightforward version re-scanned
    every episode for every day of every window, which is fine for a week of
    data and takes a quarter of an hour after a year.
    """
    prepared = prepare(rows)
    if not prepared:
        return []

    first_day = min(r["start"] for r in prepared)
    last_day = today or max(r["end"] for r in prepared)

    # TODAY IS NOT A DAY YET
    #
    # An episode leaves the live set on its last_seen, so one that has not
    # been swept yet today looks like it ended yesterday. At 02:15 on the
    # first full day, 319 of 4721 episodes were still active with last_seen
    # on the previous day, and supply read 4402 instead of 4721 - a 7%
    # undercount that fills itself in as the day's sweeps land.
    #
    # It is right for a day that is over and wrong for the one in progress,
    # and nothing in the numbers says which is which. So the row says.
    # Prague's today, matching the days the series is bucketed into. Under
    # UTC, between 22:00 and midnight local time this said "yesterday" about
    # a day that had already ended here, and marked the current day complete
    # two hours early.
    complete_through = (today or cas.today()) - timedelta(days=1)

    by_segment: dict[str, list[dict]] = defaultdict(list)
    for row in prepared:
        by_segment[row["segment"]].append(row)
        # A roll-up across property types but NEVER across sale and rent: a
        # rent of 25 000 a month averaged with a sale price of nine million
        # is not a number about anything.
        by_segment["vse/" + row["segment"].split("/", 1)[-1]].append(row)

    span = (last_day - first_day).days + 1
    out: list[dict] = []

    for segment, segment_rows in sorted(by_segment.items()):
        starts: dict[int, list[dict]] = defaultdict(list)
        ends: dict[int, list[dict]] = defaultdict(list)
        for row in segment_rows:
            starts[(row["start"] - first_day).days].append(row)
            if row["gone"]:
                ends[(row["end"] - first_day).days].append(row)

        # Supply per day, and its running total, so the mean across any
        # window is two lookups rather than a scan.
        supply = [0] * span
        running = [0] * (span + 1)
        live: dict[int, dict] = {}
        leaving_on: dict[int, list[int]] = defaultdict(list)
        for index, row in enumerate(segment_rows):
            leaving_on[min((row["end"] - first_day).days, span - 1)].append(index)
        for offset in range(span):
            for row in starts.get(offset, []):
                live[id(row)] = row
            supply[offset] = len(live)
            running[offset + 1] = running[offset] + supply[offset]
            for index in leaving_on.get(offset, []):
                live.pop(id(segment_rows[index]), None)

        # Walked again, this time keeping the live set so the day's stock is
        # carried forward instead of re-derived.
        live = {}
        for offset in range(span):
            day = first_day + timedelta(days=offset)
            for row in starts.get(offset, []):
                live[id(row)] = row
            on_market = list(live.values())

            if on_market:
                levels = levels_on(day, on_market)
                for window in windows:
                    low = max(0, offset - window + 1)
                    arrived = [r for d in range(low, offset + 1)
                               for r in starts.get(d, [])]
                    left = [r for d in range(low, offset + 1)
                            for r in ends.get(d, [])]
                    covered = offset - low + 1
                    supply_mean = (running[offset + 1] - running[low]) / covered
                    out.append(measure(day, window, segment, first_day,
                                       on_market, arrived, left, supply_mean,
                                       levels, covered, complete_through))

            for index in leaving_on.get(offset, []):
                live.pop(id(segment_rows[index]), None)
    return out


def summary(series: list[dict], window: int = 30) -> list[dict]:
    """The latest value of each indicator beside its value a window ago."""
    rows = [r for r in series if r["okno_dnu"] == window]
    if not rows:
        return []
    days = sorted({r["den"] for r in rows})
    latest, earlier = days[-1], days[max(0, len(days) - 1 - window)]
    by_key = {(r["den"], r["segment"]): r for r in rows}

    out = []
    for segment in sorted({r["segment"] for r in rows}):
        now = by_key.get((latest, segment))
        then = by_key.get((earlier, segment))
        if not now:
            continue
        for field in FIELDS[3:]:
            a, b = now.get(field), (then or {}).get(field)
            change = ""
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b:
                change = round(100.0 * (a - b) / b, 2)
            out.append({
                "segment": segment, "okno_dnu": window, "ukazatel": field,
                "ted": a if a is not None else "",
                "tehdy": b if b is not None else "",
                "zmena_pct": change,
            })
    return out


def export(data_dir: Path = storage.DATA_DIR, window: int = 30) -> dict:
    history = data_dir / "csv" / "historie_nemovitosti.csv"
    if not history.exists():
        episodes.export(data_dir)
    with open(history, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    series = daily(rows)
    out_dir = data_dir / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)

    daily_path = out_dir / "trh_denne.csv"
    with open(daily_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in series:
            writer.writerow({k: ("" if v is None else v) for k, v in row.items()})

    summary_path = out_dir / "trh_souhrn.csv"
    with open(summary_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["segment", "okno_dnu", "ukazatel", "ted", "tehdy", "zmena_pct"])
        writer.writeheader()
        writer.writerows(summary(series, window))

    return {"days": len({r["den"] for r in series}), "rows": len(series),
            "daily": str(daily_path), "summary": str(summary_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--window", type=int, default=30)
    args = parser.parse_args()

    stats = export(Path(args.data_dir), args.window)
    print(f"[market] {stats['daily']}: {stats['rows']} rows over "
          f"{stats['days']} days; summary in {stats['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
