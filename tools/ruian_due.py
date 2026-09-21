"""Does the address index need rebuilding?

    python -m tools.ruian_due --index store/data/ruian_praha.csv.gz
    -> prints "go=true" or "go=false" for $GITHUB_OUTPUT

Asks the artifact, not the schedule. Every guard in this project works this
way and for the same reason: run history answers "did the workflow fire",
which is a different question from "does the output exist and is it current",
and the two came apart every time they were allowed to. A stale tag once made
the weekly backup skip a week that had no archive.

So this reads the sidecar the builder writes and compares the export it names
against the newest one that could exist - the end of last month, since the
register publishes monthly and today's date 404s.

A LATE PUBLICATION IS NOT A REASON TO KEEP ASKING

If the newest export is not out yet the builder falls back to the month
before and writes the same export date again. Left at that, every attempt in
the window would download three megabytes to learn the same thing.

An age tolerance was the obvious fix and is the wrong one: any tolerance wide
enough not to spin on the first of the month is also wide enough that the
index never comes due during a window early in the month. So the sidecar
records the day the builder last TRIED, and this allows one attempt a day.
An export that has not appeared is retried tomorrow; one that has stops the
retries by being newer than the check wants. No tolerance to tune, and an
index that is never successfully rebuilt stays due every single day rather
than going quiet.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

from common import cas

def previous_month_end(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


def is_due(meta_text, today: date) -> tuple:
    """(due, why). `meta_text` is the sidecar's contents, or None."""
    if meta_text is None:
        return True, "no index has been built"

    try:
        meta = json.loads(meta_text)
    except (TypeError, ValueError):
        return True, "the index metadata is not readable"

    # "[]" and "null" are valid JSON and neither has a .get. Reached by the
    # tests before it was reached in production, which is the cheaper order.
    if not isinstance(meta, dict):
        return True, "the index metadata is not an object"

    stamp = meta.get("export")
    try:
        export = date.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return True, f"the index metadata names no usable export ({stamp!r})"

    if not meta.get("rows"):
        return True, "the index metadata reports no rows"

    if export >= previous_month_end(today):
        return False, f"the index is built from {export}, the newest there is"

    # Older than it could be - but a build already tried today, so the newer
    # export is not published and asking again this afternoon will not change
    # that. Tomorrow it might.
    if str(meta.get("built_at")) == today.isoformat():
        return False, (f"the index is built from {export}, and today's "
                       "attempt already found nothing newer")

    age = (today - export).days
    return True, (f"the index is built from {export}, {age} days old, and "
                  "nothing has been tried today")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True)
    parser.add_argument("--today", default=None,
                        help="ISO date, for the tests")
    args = parser.parse_args(argv)

    from tools import build_ruian_index
    meta_path = build_ruian_index.meta_path_for(args.index)
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta_text = handle.read()
    except OSError:
        meta_text = None

    today = (date.fromisoformat(args.today) if args.today else cas.today())
    due, why = is_due(meta_text, today)

    print(f"go={'true' if due else 'false'}")
    print(f"# {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
