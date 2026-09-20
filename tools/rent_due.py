"""Has rent already been collected this week?

    python -m tools.rent_due --data-dir store/data   -> go=true / go=false

The rent pass runs once a week, and the guard used to ration it the way it
rations the hourly sale sweep: by asking the Actions API how long ago this
workflow last ran a real run, with a six-day floor.

That failed on the first Sunday it mattered. The floor measured from a manual
test run on the Wednesday - 3.7 days, under the floor - so the pass was
stopped in ten seconds and the week's rent was lost. Worse, the run it
measured against had left nothing behind: the dataset was rebuilt after it,
and it holds no rent at all.

A weekly job cannot take its schedule from when a button was last pressed.
It has to ask what it is for: is there rent in the dataset from this week?
Nothing else answers that - not the run history, not the clock.

The week is the ISO week in UTC (Monday 00:00), which is what makes a manual
run on Wednesday count for that week and not for the next one.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from common import storage

RENT = "pronajem"


def _seen_on(row: dict) -> Optional[date]:
    stamp = (row.get("last_seen_at") or "").strip()
    if not stamp:
        return None
    try:
        return date.fromisoformat(stamp[:10])
    except ValueError:
        return None


def collected_week(path: Path) -> Optional[tuple[int, int]]:
    """The ISO year/week of the most recent rent sighting, or None."""
    if not path.exists():
        return None
    newest: Optional[date] = None
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("transaction_type") or "").strip() != RENT:
                continue
            seen = _seen_on(row)
            if seen is not None and (newest is None or seen > newest):
                newest = seen
    if newest is None:
        return None
    iso = newest.isocalendar()
    return iso[0], iso[1]


def due(path: Path, now: Optional[datetime] = None) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    this_week = now.isocalendar()[:2]
    try:
        collected = collected_week(path)
    except (OSError, csv.Error) as exc:
        # An unreadable dataset is not evidence that rent was collected, and
        # refusing on it would stop the weekly pass for good.
        return True, f"Could not read {path.name} ({exc})."
    if collected is None:
        return True, "The dataset holds no rent at all."
    if collected == tuple(this_week):
        return False, f"Rent already collected in week {collected[0]}-W{collected[1]:02d}."
    return True, (f"Last rent is from week {collected[0]}-W{collected[1]:02d}, "
                  f"this is {this_week[0]}-W{this_week[1]:02d}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    args = parser.parse_args()

    go, reason = due(Path(args.data_dir) / "listings.csv")
    print(reason, file=sys.stderr)
    print(f"go={'true' if go else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
