"""Has the report for the week that just ended been written?

    python -m tools.report_due --data-dir store/data   -> go=true / go=false

Attempts arrive across a window on Monday, and again on Tuesday when Monday
produced nothing. This lets one of them through.

It asks for the week's own file. reports/2026-W38.md is the report on the
week to Sunday 20 September whether it was rendered on the Monday or on the
Tuesday after a failure, so the retry finds Monday's work if there was any
and does it if there was not - without a floor, a clock or a stored marker.

That is a change from measuring the age of REPORT.md. The age worked while
the report ran once a week at a fixed hour; with a retry a day later it
cannot tell "yesterday's attempt succeeded" from "yesterday's attempt was
the failure I am retrying".

Every unreadable case returns due=true. A missing report costs a week's
page, which the next run cannot recover because the series has moved on; a
duplicate costs thirty seconds and overwrites itself.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from common import cas, storage


def due(root: Path, today: Optional[date] = None) -> tuple[bool, str]:
    week = cas.closed_week(today)
    path = root / "reports" / f"{week}.md"
    try:
        exists = path.is_file() and path.stat().st_size > 0
    except OSError as exc:
        return True, f"Could not look for {path.name} ({exc})."
    if exists:
        return False, f"The report on {week} is already written."
    return True, f"No report on {week} yet."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    args = parser.parse_args()

    go, reason = due(Path(args.data_dir).parent)
    print(reason, file=sys.stderr)
    print(f"go={'true' if go else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
