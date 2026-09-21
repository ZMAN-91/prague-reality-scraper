"""Has the city-wide pass already run today?

    python -m tools.night_due --logs-dir store/logs   -> go=true / go=false

The hourly pass walks two districts. Everything else in Prague - and every
absence, since absence marking only means anything on a walk that covered the
population it is judging - depends on one city-wide pass a day.

Attempts arrive across a window, because GitHub's scheduler drops most of
what it is asked for and the external waker fires every fifteen minutes. This
is what lets exactly one of them through.

It asks the run log, which is the pass's own record of itself: a city-scope
run whose start falls on today's Prague date. Not a marker file, not the
Actions history - the thing that only exists because the work happened.

Every unreadable case returns due=true. A night without a city pass costs a
day of coverage outside the watched districts; a duplicate costs one extra
walk. Those are not the same size of mistake.
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from common import cas, storage

CITY = "city"


def last_city_day(logs_dir: Path) -> Optional[date]:
    """The Prague day of the most recent city-wide run, or None."""
    newest: Optional[date] = None
    for path in sorted(glob.glob(str(logs_dir / "*.jsonl"))):
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Runs written before scopes existed were all city-wide, and
                # reading them as such is right: they did walk the whole city.
                if entry.get("scope", CITY) != CITY:
                    continue
                day = cas.date_of(entry.get("started_at"))
                if day is not None and (newest is None or day > newest):
                    newest = day
    return newest


def due(logs_dir: Path, today: Optional[date] = None) -> tuple[bool, str]:
    today = today or cas.today()
    # No try/except here: last_city_day already skips a file it cannot open,
    # so an unreadable log yields None and falls into the branch below. An
    # outer handler would be unreachable - and a test was written against it
    # that passed whether the handler existed or not.
    last = last_city_day(logs_dir)
    if last is None:
        return True, "No city-wide pass has ever been logged (or the log could not be read)."
    if last >= today:
        return False, f"City-wide pass already ran today ({last})."
    return True, f"Last city-wide pass was {last}, today is {today}."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--logs-dir", default=str(storage.LOGS_DIR))
    args = parser.parse_args()

    go, reason = due(Path(args.logs_dir))
    print(reason, file=sys.stderr)
    print(f"go={'true' if go else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
