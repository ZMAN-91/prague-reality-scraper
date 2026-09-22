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

# How long a source may go without completing a full sweep before its
# removals have genuinely stopped being recorded. Every source completes one
# at least daily - bezrealitky every run from its sitemap, iDNES on the
# nightly city pass, sreality on the daily city-wide index walk - so this is
# a day with a quarter of a day of slack for a late or skipped window.
MAX_HOURS_WITHOUT_COMPLETE_SWEEP = 30.0

# What a log line from before scopes were recorded gets called, so an old
# log still says something rather than silently counting as complete.
SCOPE_UNKNOWN = "(scope not recorded)"

# Sunday 00:00-05:00 UTC belongs to the rent pass: the sale cron skips it and
# the external waker sleeps through it, both on purpose. Sale sweeps stop for
# five hours every Sunday, which is not a broken waker - and a check that
# says it is would cry wolf once a week until nobody read it any more.
RENT_WINDOW_WEEKDAY = 6  # Sunday, as datetime.weekday() counts
RENT_WINDOW_HOURS = (0, 5)


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


def reserved_hours(start: datetime, end: datetime) -> float:
    """Hours between the two that the rent window is entitled to."""
    if end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.date():
        if day.weekday() == RENT_WINDOW_WEEKDAY:
            opens = datetime.combine(day, datetime.min.time(), timezone.utc) \
                + timedelta(hours=RENT_WINDOW_HOURS[0])
            closes = datetime.combine(day, datetime.min.time(), timezone.utc) \
                + timedelta(hours=RENT_WINDOW_HOURS[1])
            overlap = min(end, closes) - max(start, opens)
            total += max(0.0, overlap.total_seconds() / 3600)
        day += timedelta(days=1)
    return total


def check_gap(runs: list[dict], now: datetime) -> list[str]:
    """How long since the last sale sweep - the waker's pulse.

    Minus the hours sale is not supposed to be running in. Without that this
    reports a broken waker every Sunday morning, because the five hours the
    rent pass owns look exactly like five hours of nothing happening.
    """
    sales = of_kind(runs, "prodej")
    if not sales:
        return []
    last = parse(sales[-1].get("started_at"))
    if last is None:
        return []
    hours = (now - last).total_seconds() / 3600 - reserved_hours(last, now)
    if hours > MAX_GAP_HOURS:
        return [f"Nothing collected for {hours:.1f} hours of the hours it "
                f"should have (last sweep {last:%Y-%m-%d %H:%M} UTC). "
                "The waker is not waking it."]
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
        # Never having run is the worse version of having run too long ago,
        # and this check used to stay silent about it - which it did for the
        # four days the rent pass was being stopped by its own guard. It only
        # counts once there is enough history for a weekly job to have owed
        # us a pass: a dataset two days old is not missing its week yet.
        oldest = parse((runs[0] if runs else {}).get("started_at"))
        if oldest is None:
            return []
        days = (now - oldest).total_seconds() / 86400
        if days > RENT_OVERDUE_DAYS:
            return [f"The rent pass has never run, and there has been "
                    f"{days:.1f} days of collection. It is weekly."]
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
        if source.get("fetched") == 0:
            out.append(f"{name} fetched nothing at all - the portal changed, "
                       "or it is blocking us.")
    return out


def check_sweep_freshness(runs: list[dict], now: datetime) -> list[str]:
    """How long since each source last got all the way round.

    This replaces a per-run test - "did THIS run finish its sweep" - which
    was wrong in principle and cost nine failed jobs and nine emails before
    anyone looked at why.

    Not finishing a sweep in one run is the normal, designed state for two
    of the three sources. iDNES rotates through ~257 index pages, so an
    hourly run sees perhaps a tenth of Prague and reports itself incomplete
    by construction. sreality's hourly pass walks two boroughs on purpose
    and must NOT claim completeness - that claim is exactly the bug that
    marked 1,201 live listings missing. Both are correct behaviour, and the
    old check called both of them a failure, every hour, for ever.

    health.py's own docstring says a finding that repeats on every run
    trains you to ignore the whole file. It was right, and this file was
    the thing that proved it.

    What IS worth an email is a source that has not completed a sweep in a
    long time: then removals really have stopped being recorded. Every
    source here completes one at least daily - bezrealitky every run from
    its sitemap, iDNES on the nightly city pass, sreality on the daily
    city-wide index walk - so a day and a quarter of silence is a real
    fault and nothing less is.
    """
    # Per SCOPE, not per source. `run_complete` is
    # `bool(scopes) and not real_errors`, so one bad category takes the
    # whole source down with it: a suspicious drop in sreality's
    # dum/pronajem - 65 active adverts, small enough to be volatile and big
    # enough to trip the guard - makes the daily walk log itself as
    # incomplete even though the other three categories finished cleanly.
    # Thirty hours later this would start emailing about a sweep that
    # happened. That is the same over-broad failure this check was just
    # rewritten to stop, one layer down.
    #
    # `scopes_absence_marked` is the positive, precise statement: these are
    # the categories whose absence WAS marked, which is the only thing a
    # completed sweep buys.
    # Only scopes that have completed at least once get an entry. Creating
    # one from a run that completed NOTHING is what the first version of
    # this did, and it produced "sreality has never completed a sweep" on
    # every run for ever - the hourly area pass completes no scope by
    # design, so the empty entry could never be filled. That is the exact
    # failure being fixed here, reintroduced one level down. An end-to-end
    # run caught it; the unit tests did not.
    last_complete: dict[tuple[str, str], datetime] = {}
    seen_sources: set[str] = set()
    for run in runs:
        when = parse(run.get("started_at"))
        for name, source in (run.get("sources") or {}).items():
            seen_sources.add(name)
            done = source.get("scopes_absence_marked")
            if done is None:
                # An older log line, from before scopes were recorded.
                done = [SCOPE_UNKNOWN] if source.get("run_complete") else []
            if when is None:
                continue
            for scope in done:
                key = (name, scope)
                if key not in last_complete or when > last_complete[key]:
                    last_complete[key] = when

    out = []
    # A source that has completed nothing at all in the whole log is broken
    # rather than merely partial, and is the one case the per-scope view
    # cannot see: with no completed scope there is no scope to report on.
    for name in sorted(seen_sources - {n for n, _ in last_complete}):
        out.append(f"{name} has never completed a sweep in any logged run, "
                   "so nothing it misses can be treated as gone.")

    for (name, scope) in sorted(last_complete):
        when = last_complete[(name, scope)]
        where = name if scope == SCOPE_UNKNOWN else f"{name} {scope}"
        hours = (now - when).total_seconds() / 3600
        if hours > MAX_HOURS_WITHOUT_COMPLETE_SWEEP:
            out.append(
                f"{where} has not completed a sweep for {hours:.1f} hours "
                f"(last {when:%Y-%m-%d %H:%M}) - removals stopped being "
                "recorded that long ago.")
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
            + check_last_run(runs) + check_sweep_freshness(runs, now)
            + check_days(data_dir, now))


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
