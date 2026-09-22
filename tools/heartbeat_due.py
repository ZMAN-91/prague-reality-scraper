"""Is a heartbeat due?

    python -m tools.heartbeat_due --status STATUS.md   -> "go=true" / "go=false"

GitHub disables scheduled workflows in a public repository after 60 days
without repository activity, and every commit this collector makes goes to
the private data repository. The heartbeat exists to push something here.

It used to ask for one attempt a week. That is the one thing this project
knows not to do: GitHub's scheduler delivered 1 of 32 attempts when measured,
so a weekly request is a coin toss repeated fifty times a year, and a bad run
of it inside a sixty-day window switches the whole collection off silently -
no failure, nothing red, just an hourly scrape that stops happening.

So the workflow now asks several times a day and this decides whether the
attempt should do anything, by reading the file the last heartbeat wrote.
Same shape as every other guard here: ask the artifact, never the schedule.

Weekly remains the CADENCE. 52 commits a year, not 8,760 - the point is to
stay far inside the window while staying out of the log.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone

from common import cas

#: How long a heartbeat is good for. Comfortably inside GitHub's sixty days,
#: and long enough that four attempts a day produce one commit a week.
MAX_AGE_DAYS = 7

STAMP = re.compile(r"Posledn\S* heartbeat:\s*\*\*(\d{4}-\d{2}-\d{2})")


def last_beat(status_text):
    """The date the last heartbeat wrote, or None."""
    if not status_text:
        return None
    found = STAMP.search(status_text)
    if not found:
        return None
    try:
        return datetime.strptime(found.group(1), "%Y-%m-%d").replace(
            tzinfo=timezone.utc).date()
    except ValueError:
        return None


def is_due(status_text, today=None):
    """(due, why)."""
    today = today or cas.today()
    beat = last_beat(status_text)
    if beat is None:
        return True, "no heartbeat has been recorded"
    age = (today - beat).days
    if age < 0:
        return True, f"the last heartbeat is dated {beat}, in the future"
    if age >= MAX_AGE_DAYS:
        return True, f"the last heartbeat was {age} days ago"
    return False, f"the last heartbeat was {age} days ago"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--status", default="STATUS.md")
    parser.add_argument("--today", default=None)
    args = parser.parse_args(argv)

    try:
        with open(args.status, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        text = None

    from datetime import date
    today = date.fromisoformat(args.today) if args.today else None
    due, why = is_due(text, today)
    print(f"go={'true' if due else 'false'}")
    print(f"# {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
