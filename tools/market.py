"""Ten market indicators, computed per day so trends are visible.

A single number tells you nothing. "Average price 9.3M" is not a fact about
the market, it is a fact about today; the question is always whether it is
higher than last month and whether the thing driving it is prices or mix. So
everything here is a daily series, and every indicator is computed the same
way for every day of history, which is what makes two dates comparable.

    python -m tools.market            -> data/csv/trh_denne.csv
                                         data/csv/trh_souhrn.csv

WHAT EACH INDICATOR IS FOR

  1  nabidka            How many properties are on the market that day. The
                        denominator for everything else, and on its own the
                        clearest sign of a market loosening or tightening.
  2  nove               New arrivals that day. Supply being created.
  3  zmizele            Departures that day. Supply being absorbed - or given
                        up on; the data cannot say which.
  4  absorpce_pct       Departures as a share of what was on the market. The
                        single best "is it selling?" number: 2% a day empties
                        the market in seven weeks, 0.2% in two years.
  5  cena_median        Median asking price of what is on the market. Median,
                        not mean - one 40M villa moves a mean and tells you
                        nothing about the market it is in.
  6  cena_m2_median     The same per square metre, which is the only way to
                        compare across a changing mix of sizes.
  7  cena_zmizelych     Median price of what left that day, and separately of
     cena_rychlych      what left within two weeks. Quick departures are the
                        ones most likely to have actually sold, so the gap
                        between these two is a read on what the market will
                        actually pay versus what it is being asked.
  8  dnu_na_trhu_median Median days on market of everything that left. Rising
                        means sellers are stuck; falling means they are not.
  9  zlevnilo_pct       Share of what is on the market that has cut its price
     zlevneni_prumer    at least once, and the average number of cuts. This
                        is the seller-capitulation indicator, and it turns
                        before prices do.
 10  sleva_median_pct   Median discount among those that cut. How deep sellers
                        have to go, as opposed to how many of them go.

HOW TO READ THEM TOGETHER

Supply up + absorption down + discounting up is a market turning against
sellers, and those three move in that order. The reverse, in the same order,
is one turning for them. Median price is the slowest of the four and the one
most contaminated by mix, so it confirms rather than warns.

WHAT NONE OF IT CAN TELL YOU

Whether anything sold. No portal publishes that, so a departure is a
departure; "quick departures" is a proxy, and a good one, but a proxy.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable, Optional

from common import storage
from tools import episodes

# A departure within a fortnight is the strongest signal available that
# something actually sold rather than being withdrawn: a seller who gives up
# rarely does it in two weeks.
QUICK_DAYS = 14

# Below this many properties a median is noise, not a measurement.
MIN_SAMPLE = 5

FIELDS = [
    "den", "segment",
    "nabidka", "nove", "zmizele", "absorpce_pct",
    "cena_median", "cena_m2_median",
    "cena_zmizelych", "cena_rychlych",
    "dnu_na_trhu_median",
    "zlevnilo_pct", "zlevneni_prumer", "sleva_median_pct",
]


def as_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def as_num(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def median(values: Iterable) -> Optional[float]:
    kept = [v for v in values if v is not None]
    if len(kept) < MIN_SAMPLE:
        # Reported as empty rather than as a number, so a thin day reads as
        # "not measured" instead of as a swing in the market.
        return None
    return round(statistics.median(kept), 2)


def share(part: int, whole: int) -> Optional[float]:
    if whole < MIN_SAMPLE:
        return None
    return round(100.0 * part / whole, 2)


def segment_of(row: dict) -> str:
    return f"{row.get('property_type') or '?'}/{row.get('transaction_type') or '?'}"


def price_at(row: dict) -> Optional[float]:
    """The asking price to attribute to this episode.

    The last one: an episode that cut its price twice is, today, asking the
    third figure, and the first two are history rather than the state of the
    market.
    """
    return as_num(row.get("last_price")) or as_num(row.get("first_price"))


def daily(rows: list[dict], today: Optional[date] = None) -> list[dict]:
    """One row per (day, segment), over the whole span the data covers.

    Built from episodes rather than from listings, so a property advertised by
    three agencies counts once and a property re-listed three times counts as
    one continuous episode. Both of those, left alone, distort every number
    here in the same direction: they inflate supply and shorten time on market.
    """
    spans = []
    for row in rows:
        start, end = as_date(row.get("first_seen")), as_date(row.get("last_seen"))
        if not start or not end or end < start:
            continue
        spans.append((start, end, row))
    if not spans:
        return []

    first_day = min(s for s, _, _ in spans)
    last_day = today or max(e for _, e, _ in spans)

    # Bucket by day once, rather than scanning every episode for every day:
    # a year of history against a year of episodes is otherwise quadratic.
    starts: dict[date, list[dict]] = defaultdict(list)
    ends: dict[date, list[dict]] = defaultdict(list)
    for start, end, row in spans:
        starts[start].append(row)
        if row.get("outcome") == "removed":
            ends[end].append(row)

    live: dict[str, tuple[date, dict]] = {}
    out: list[dict] = []
    day = first_day
    while day <= last_day:
        for row in starts.get(day, []):
            live[row["property_key"] + "|" + str(row.get("episode"))] = (day, row)

        leaving = ends.get(day, [])
        by_segment: dict[str, dict] = defaultdict(lambda: {"live": [], "new": [],
                                                           "gone": []})
        for _, row in live.values():
            by_segment[segment_of(row)]["live"].append(row)
        for row in starts.get(day, []):
            by_segment[segment_of(row)]["new"].append(row)
        for row in leaving:
            by_segment[segment_of(row)]["gone"].append(row)

        for segment, group in sorted(by_segment.items()):
            out.append(measure(day, segment, group))
        if by_segment:
            out.append(measure(day, "vse", {
                "live": [r for g in by_segment.values() for r in g["live"]],
                "new": [r for g in by_segment.values() for r in g["new"]],
                "gone": [r for g in by_segment.values() for r in g["gone"]],
            }))

        for row in leaving:
            live.pop(row["property_key"] + "|" + str(row.get("episode")), None)
        day += timedelta(days=1)
    return out


def measure(day: date, segment: str, group: dict) -> dict:
    on_market, arrived, left = group["live"], group["new"], group["gone"]

    discounted = [r for r in on_market if (as_num(r.get("discount_czk")) or 0) > 0]
    cuts = [as_num(r.get("price_changes")) or 0 for r in on_market]
    quick = [r for r in left
             if (as_num(r.get("days_on_market")) or 0) <= QUICK_DAYS]

    return {
        "den": day.isoformat(),
        "segment": segment,
        "nabidka": len(on_market),
        "nove": len(arrived),
        "zmizele": len(left),
        "absorpce_pct": share(len(left), len(on_market)),
        "cena_median": median(price_at(r) for r in on_market),
        "cena_m2_median": median(as_num(r.get("price_per_m2_last")) for r in on_market),
        "cena_zmizelych": median(price_at(r) for r in left),
        "cena_rychlych": median(price_at(r) for r in quick),
        "dnu_na_trhu_median": median(as_num(r.get("days_on_market")) for r in left),
        "zlevnilo_pct": share(len(discounted), len(on_market)),
        "zlevneni_prumer": (round(sum(cuts) / len(cuts), 2)
                            if len(cuts) >= MIN_SAMPLE else None),
        "sleva_median_pct": median(as_num(r.get("discount_pct")) for r in discounted),
    }


def summary(series: list[dict], window: int = 30) -> list[dict]:
    """The latest value of each indicator beside its value `window` days ago,
    which is the smallest thing that is still a trend rather than a reading."""
    if not series:
        return []
    days = sorted({r["den"] for r in series})
    latest, earlier = days[-1], days[max(0, len(days) - 1 - window)]
    by_key = {(r["den"], r["segment"]): r for r in series}

    out = []
    for segment in sorted({r["segment"] for r in series}):
        now = by_key.get((latest, segment))
        then = by_key.get((earlier, segment))
        if not now:
            continue
        for field in FIELDS[2:]:
            a, b = now.get(field), (then or {}).get(field)
            change = ""
            if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b:
                change = round(100.0 * (a - b) / b, 2)
            out.append({
                "segment": segment, "ukazatel": field,
                "ted": a if a is not None else "",
                "pred_dny": window,
                "tehdy": b if b is not None else "",
                "zmena_pct": change,
            })
    return out


def export(data_dir: Path = storage.DATA_DIR, window: int = 30) -> dict:
    history = data_dir / "csv" / "historie_nemovitosti.csv"
    if not history.exists():
        # Built from episodes, so build them if nobody has.
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

    summary_rows = summary(series, window)
    summary_path = out_dir / "trh_souhrn.csv"
    with open(summary_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["segment", "ukazatel", "ted", "pred_dny", "tehdy", "zmena_pct"])
        writer.writeheader()
        writer.writerows(summary_rows)

    return {"days": len({r["den"] for r in series}), "rows": len(series),
            "daily": str(daily_path), "summary": str(summary_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--window", type=int, default=30,
                        help="days back to compare against in the summary")
    args = parser.parse_args()

    stats = export(Path(args.data_dir), args.window)
    print(f"[market] {stats['daily']}: {stats['rows']} rows over "
          f"{stats['days']} days; summary in {stats['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
