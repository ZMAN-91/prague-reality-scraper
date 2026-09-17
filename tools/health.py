"""Is the collection actually running, and is what it produces sane?

    python -m tools.health                 -> findings on stdout, exit 1 if any

The scraper already fails loudly when a run goes wrong. What it cannot see is
the run that never happened: GitHub's scheduler dropped all but 4 of roughly
150 attempts over two days, and the only symptom was silence. Silence is the
failure mode this looks for, from the first run after it ends.

WHAT IT CANNOT DO, AND WHY THAT IS FINE

Nothing inside a run can notice that runs have stopped - if the collection is
dead, this is dead with it. It reports the gap on the first run AFTER one,
which is when there is finally something to report it to. A collection that
never comes back needs a watcher outside GitHub, and the only honest thing to
say about that is that this is not one.

EVERY CHECK IS ABOUT THE PRESENT

A finding that describes last Tuesday would repeat on every run forever and
train you to ignore the whole file. So each check asks about the latest
interval or the current state, and clears itself once that is healthy again.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import storage

# The waker aims for one sweep an hour. Three hours means roughly three
# missed attempts in a row, which is a broken waker rather than bad luck.
MAX_GAP_HOURS = 3.0

# Two sweeps closer than this means the guard let one through it should have
# stopped, and the portals are being swept harder than this project agreed to.
MIN_SPACING_MINUTES = 40

# The rent pass is weekly, so it is overdue only once a week has passed with
# room to spare.
RENT_OVERDUE_DAYS = 8.5


def parse(stamp) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None


def load_runs(logs_dir: Path) -> list[dict]:
    """Every run ever logged, oldest first."""
    runs = []
    for path in sorted(glob.glob(str(logs_dir / "*.jsonl"))):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    runs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    runs.sort(key=lambda r: str(r.get("started_at") or ""))
    return runs


def of_kind(runs: list[dict], transaction: str) -> list[dict]:
    return [r for r in runs if transaction in (r.get("transactions") or [])]


def check_gap(runs: list[dict], now: datetime) -> list[str]:
    """How long since the last sale sweep - the waker's pulse."""
    sales = of_kind(runs, "prodej")
    if not sales:
        return []
    last = parse(sales[-1].get("started_at"))
    if last is None:
        return []
    hours = (now - last).total_seconds() / 3600
    if hours > MAX_GAP_HOURS:
        return [f"Nothing collected for {hours:.1f} hours (last sweep "
                f"{last:%Y-%m-%d %H:%M} UTC). The waker is not waking it."]
    return []


def check_spacing(runs: list[dict]) -> list[str]:
    """Two sweeps too close means the guard stopped rationing."""
    sales = of_kind(runs, "prodej")
    if len(sales) < 2:
        return []
    previous, latest = parse(sales[-2].get("started_at")), parse(sales[-1].get("started_at"))
    if previous is None or latest is None:
        return []
    minutes = (latest - previous).total_seconds() / 60
    if minutes < MIN_SPACING_MINUTES:
        return [f"Two sweeps {minutes:.0f} minutes apart ({previous:%H:%M} and "
                f"{latest:%H:%M} UTC). The guard is not rationing; the portals "
                "are being swept harder than agreed."]
    return []


def check_rent(runs: list[dict], now: datetime) -> list[str]:
    rents = of_kind(runs, "pronajem")
    if not rents:
        return []
    last = parse(rents[-1].get("started_at"))
    if last is None:
        return []
    days = (now - last).total_seconds() / 86400
    if days > RENT_OVERDUE_DAYS:
        return [f"The rent pass has not run for {days:.1f} days (last "
                f"{last:%Y-%m-%d}). It is weekly, so it has missed a week."]
    return []


def check_last_run(runs: list[dict]) -> list[str]:
    """The newest run's own account of itself."""
    if not runs:
        return ["No run has ever been logged."]
    last = runs[-1]
    out = []
    for name, source in (last.get("sources") or {}).items():
        errors = source.get("errors") or []
        if errors:
            out.append(f"{name} reported {len(errors)} error(s), first: {errors[0]}")
        if not source.get("run_complete"):
            out.append(f"{name} did not finish its sweep, so nothing it "
                       "missed can be treated as gone.")
        if source.get("fetched") == 0:
            out.append(f"{name} fetched nothing at all - the portal changed, "
                       "or it is blocking us.")
    return out


def check_days(data_dir: Path, now: datetime) -> list[str]:
    """A day missing from the series is a day nobody collected."""
    path = data_dir / "csv" / "trh_denne.csv"
    if not path.exists():
        return []
    with open(path, encoding="utf-8", newline="") as handle:
        days = sorted({row["den"] for row in csv.DictReader(handle) if row.get("den")})
    if len(days) < 2:
        return []
    first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    have = set(days)
    missing = [(first + timedelta(days=n)).isoformat()
               for n in range((last - first).days + 1)
               if (first + timedelta(days=n)).isoformat() not in have]
    if missing:
        shown = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        return [f"{len(missing)} day(s) missing from the series: {shown}"]
    return []


def report(data_dir: Path, logs_dir: Path, now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    runs = load_runs(logs_dir)
    return (check_gap(runs, now) + check_spacing(runs) + check_rent(runs, now)
            + check_last_run(runs) + check_days(data_dir, now))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--logs-dir", default=None)
    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    logs_dir = Path(args.logs_dir) if args.logs_dir else data_dir.parent / "logs"

    findings = report(data_dir, logs_dir)
    if not findings:
        print("[health] nothing to report")
        return 0
    for finding in findings:
        print(f"[health] {finding}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
