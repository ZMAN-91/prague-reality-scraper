"""Measure the dataset's quality, record it, and fail when it collapses.

    python -m tools.quality --data-dir store/data [--record]

WHY

Several numbers in this project are computed, printed into a workflow log,
and read by nobody. The district cross-check is the clearest case: it exists
precisely to catch a broken coordinate conversion, which would still produce
matches, still with plausible distances, and still look entirely normal - and
it reports into a log that is only opened when something else has already
gone wrong. A measurement nobody reads is not a measurement.

So the same numbers are written down, compared against the recent past, and
turned into an exit code.

WHAT IS WATCHED

  district agreement  the register's own district for each matched building
                      against the district the portal stated. Nothing in the
                      matching uses that field, which is what makes it worth
                      checking: a coordinate conversion that broke, or a
                      pairing rule that started accepting the wrong flats,
                      shows up here and in nothing else.

  coverage            what share of listings have coordinates, a house number
                      and a postcode. These drift downward slowly and
                      legitimately - new listings arrive before they are
                      paired - so only a sharp fall is a fault.

TWO KINDS OF THRESHOLD

An absolute floor catches a collapse from any starting point. A fall relative
to the last fortnight catches a slide that never crosses the floor but is
still going the wrong way. Neither alone is enough: a floor set where today
sits would fire on every ordinary fluctuation, and a relative test alone
would accept any amount of decay as long as it arrived gradually.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from common import address as address_mod
from common import cas, dedup, ruian, storage

#: Below this the district cross-check is not reporting a bad week, it is
#: reporting something broken. Measured at 98.4% when built.
MIN_DISTRICT_AGREEMENT = 90.0

#: How far a share may fall below its own recent median before it is a fault
#: rather than drift, in percentage points.
MAX_DROP_POINTS = 10.0

#: Days of history kept, and how many of them a comparison needs.
HISTORY_DAYS = 90
MIN_DAYS_FOR_COMPARISON = 3

FILENAME = "quality.json"


def path_for(data_dir) -> Path:
    return Path(data_dir) / "state" / FILENAME


def load(data_dir) -> dict:
    try:
        with open(path_for(data_dir), encoding="utf-8") as handle:
            history = json.load(handle)
    except (OSError, ValueError):
        return {}
    return history if isinstance(history, dict) else {}


def _fold(text) -> str:
    return address_mod.strip_diacritics(text or "").lower().strip()


def measure(listings: dict, index) -> dict:
    """Today's numbers. `index` may be None when no index is built yet."""
    rows = list(listings.values())
    total = len(rows)
    if not total:
        return {"listings": 0}

    def share(predicate):
        return round(100.0 * sum(1 for r in rows if predicate(r)) / total, 2)

    out = {
        "listings": total,
        "gps_pct": share(lambda r: r.get("lat") and r.get("lon")),
        "number_pct": share(lambda r: r.get("cislo_zdroj") == ruian.SOURCE_NAME),
        "psc_pct": share(lambda r: (r.get("psc") or "").strip()),
    }

    if index is None:
        return out

    agree = disagree = 0
    for row in rows:
        if row.get("cislo_zdroj") != ruian.SOURCE_NAME:
            continue
        stated = _fold(row.get("mestska_cast"))
        if not stated:
            continue
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        found = index.match_detail(lat, lon, row.get("ulice"))
        if not found:
            continue
        _fields, point = found
        known = {_fold(point.get("cast_obce")), _fold(point.get("obvod")),
                 _fold(point.get("mestska_cast"))}
        if stated in known:
            agree += 1
        else:
            disagree += 1
    checked = agree + disagree
    if checked:
        out["district_agreement_pct"] = round(100.0 * agree / checked, 2)
        out["district_checked"] = checked
    return out


def faults(today: dict, history: dict) -> list:
    """Everything wrong with today's numbers, as sentences."""
    problems = []

    agreement = today.get("district_agreement_pct")
    if agreement is not None and agreement < MIN_DISTRICT_AGREEMENT:
        problems.append(
            f"district agreement is {agreement}%, below the {MIN_DISTRICT_AGREEMENT}% "
            f"floor over {today.get('district_checked')} rows - the "
            "coordinate conversion or the pairing rule is wrong, not the week")

    watched = ("gps_pct", "number_pct", "psc_pct", "district_agreement_pct")
    for key in watched:
        value = today.get(key)
        if value is None:
            continue
        earlier = [day[key] for day in history.values()
                   if isinstance(day, dict) and day.get(key) is not None]
        if len(earlier) < MIN_DAYS_FOR_COMPARISON:
            continue
        median = statistics.median(earlier)
        if median - value > MAX_DROP_POINTS:
            problems.append(
                f"{key} is {value}%, {median - value:.1f} points below its "
                f"median of {median}% over {len(earlier)} earlier days")
    return problems


def record(data_dir, today: dict, day=None) -> None:
    day = (day or cas.today()).isoformat()
    history = load(data_dir)
    history[day] = today
    for old in sorted(history)[:-HISTORY_DAYS]:
        del history[old]
    target = path_for(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--record", action="store_true",
                        help="append today's numbers to the history")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    listings = storage.read_listings(data_dir / "listings.csv")
    if not listings:
        print(f"No listings at {data_dir / 'listings.csv'}.", file=sys.stderr)
        return 1

    index_path = data_dir / "ruian_praha.csv.gz"
    index = ruian.Index.load(str(index_path)) if index_path.exists() else None
    if index is None:
        print("No address index yet; the district cross-check is skipped.")

    history = load(data_dir)
    today = measure(listings, index)
    for key in sorted(today):
        print(f"  {key:26s} {today[key]}")

    problems = faults(today, history)
    if args.record:
        record(data_dir, today)

    if not problems:
        print("\n[quality] nothing to report")
        return 0
    print("")
    for problem in problems:
        print(f"::error::[quality] {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
