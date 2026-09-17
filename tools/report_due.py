"""Has the weekly report already been written this week?

    python -m tools.report_due --data-dir store/data   -> go=true / go=false

The scrape guard tells a real run from a stopped one by DURATION: a sweep
takes half an hour, a refusal takes seconds. That does not work here. Writing
the report takes about thirty seconds and refusing takes about fifteen, and a
rule with that little daylight in it will one day call a real run a refusal
and stop the report for good.

So this asks the report itself. REPORT.md carries the moment it was
generated, which is the one thing that cannot be wrong about when the report
last ran.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import storage

# Six days, not seven: attempts land in a window on Monday morning, and a
# seven-day floor would push each week's report later than the last until it
# fell out of the window.
MIN_AGE_DAYS = 6.0

GENERATED_RE = re.compile(r"vygenerov[áa]no\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


def generated_at(text: str) -> Optional[datetime]:
    match = GENERATED_RE.search(text)
    if match is None:
        return None
    try:
        stamp = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return stamp.replace(tzinfo=timezone.utc)


def due(path: Path, now: Optional[datetime] = None,
        min_age_days: float = MIN_AGE_DAYS) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return True, "No report has ever been written."
    written = generated_at(path.read_text(encoding="utf-8"))
    if written is None:
        # An unreadable header is not evidence that the report is fresh, and
        # refusing on it would stop the report for good.
        return True, "Could not read when the report was generated."
    age = (now - written).total_seconds() / 86400
    if age >= min_age_days:
        return True, f"Last report {written:%Y-%m-%d %H:%M} UTC, {age:.1f} days ago."
    return False, f"Last report {written:%Y-%m-%d %H:%M} UTC, only {age:.1f} days ago."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--min-age-days", type=float, default=MIN_AGE_DAYS)
    args = parser.parse_args()

    go, reason = due(Path(args.data_dir).parent / "REPORT.md",
                     min_age_days=args.min_age_days)
    print(reason, file=sys.stderr)
    print(f"go={'true' if go else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
