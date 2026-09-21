"""The day this project means when it says "day".

Timestamps are recorded in UTC and stay that way: a moment with an offset on
it cannot be misread, and rewriting the record to a local clock would throw
away the one thing that makes two observations comparable.

What is local is the DAY. This is a dataset about the Prague market, read by
someone in Prague, so "2026-09-21" has to mean the day that person lived
through - midnight to midnight on their clock, not on Greenwich's.

The difference is not academic. Under UTC days, everything collected between
22:00 and midnight Prague time (23:00 and midnight in winter) landed on the
day before. Two hours of every evening filed under yesterday, and a weekly
report generated at Monday 00:00 Prague would have found Sunday still open
and reported a week ending Saturday.

DST is handled by the zone, not by an offset: Europe/Prague is +01:00 in
winter and +02:00 in summer, and the two switch-over days are 23 and 25
hours long. Hard-coding +02:00 would put the boundary an hour out for five
months of the year.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")


def now() -> datetime:
    """The current moment, as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def to_prague(moment: datetime) -> datetime:
    """Any aware datetime in Prague's zone. A naive one is read as UTC,
    because that is what this project stores when an offset is missing."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(PRAGUE)


def day_of(value: Optional[str]) -> str:
    """The Prague calendar day of an ISO timestamp, as YYYY-MM-DD.

    A bare date passes through unchanged. That shortcut cannot change an
    answer - Prague is east of Greenwich, so midnight UTC is always the same
    calendar day here - it is there to say plainly that a stored day is
    already a day and is not reinterpreted as a moment.

    Anything unparseable falls back to the first ten characters, which is
    what this project did before and is never worse than raising in the
    middle of a sweep.
    """
    text = str(value or "")
    if not text:
        return ""
    if len(text) == 10:
        return text
    try:
        return to_prague(datetime.fromisoformat(text)).date().isoformat()
    except ValueError:
        return text[:10]


def date_of(value: Optional[str]) -> Optional[date]:
    """Same as day_of, as a date object. None when there is nothing to read."""
    day = day_of(value)
    try:
        return date.fromisoformat(day)
    except ValueError:
        return None


def today() -> date:
    """Today in Prague."""
    return to_prague(now()).date()


def closed_week_end(today: Optional[date] = None) -> date:
    """The Sunday of the last week that has actually finished.

    The weekly report and the weekly archive are about a week, not about
    "everything up to yesterday", and the difference only shows when they run
    late. On Monday those two are the same thing; on Tuesday - the retry when
    Monday failed - "up to yesterday" quietly includes Monday, so the week
    reported is a week plus a day, and the week after it is short one.

    Anchored on the most recent Sunday strictly before today, so Monday and
    Tuesday of the same week both answer with that same Sunday.
    """
    today = today or globals()["today"]()
    # isoweekday(): Monday 1 ... Sunday 7.
    return today - timedelta(days=today.isoweekday())


def week_label(day: date) -> str:
    """The ISO week a given day falls in, "2026-W38".

    Separate from closed_week on purpose: that one takes TODAY and steps back
    to the week before, this one takes a day and names its own week. Passing
    a closed week's Sunday into closed_week applies the step twice and names
    the week before that - which is how the first version of this filed a
    Monday report under W37.
    """
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def closed_week(today: Optional[date] = None) -> str:
    """That week as an ISO label, "2026-W39" - what the report and the
    archive are named after, so both say which week they are ABOUT rather
    than which week they happened to be produced in."""
    return week_label(closed_week_end(today))
