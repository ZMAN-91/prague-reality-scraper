"""The day boundary, which used to be Greenwich's.

Under UTC days everything collected between 22:00 and midnight Prague time
was filed under the day before - two hours of every evening, every day.
"""

from datetime import date, datetime, timezone

import pytest

from common import cas


def test_an_evening_observation_belongs_to_that_evening():
    """22:30 Prague in summer is 20:30 UTC - same day either way."""
    assert cas.day_of("2026-09-21T20:30:00+00:00") == "2026-09-21"


def test_the_late_evening_no_longer_falls_back_a_day():
    """22:30 UTC is 00:30 Prague the NEXT day. This is the whole point."""
    assert cas.day_of("2026-09-21T22:30:00+00:00") == "2026-09-22"


def test_winter_moves_the_boundary_by_an_hour():
    """Prague is +01:00 in winter, so 23:30 UTC is 00:30 the next day - but
    22:30 UTC is still the same day, unlike in summer."""
    assert cas.day_of("2026-01-15T23:30:00+00:00") == "2026-01-16"
    assert cas.day_of("2026-01-15T22:30:00+00:00") == "2026-01-15"


def test_summer_and_winter_disagree_about_the_same_clock_time():
    """A fixed +02:00 offset would be an hour out for five months. 22:30 UTC
    is the next day in summer and the same day in winter."""
    assert cas.day_of("2026-07-15T22:30:00+00:00") == "2026-07-16"
    assert cas.day_of("2026-01-15T22:30:00+00:00") == "2026-01-15"


def test_a_bare_date_is_already_a_day():
    """last_seen_at is stored day-granular; there is no moment to convert."""
    assert cas.day_of("2026-09-21") == "2026-09-21"


def test_nothing_in_gives_nothing_out():
    assert cas.day_of(None) == ""
    assert cas.day_of("") == ""


def test_something_unreadable_does_not_stop_a_sweep():
    """Falling back to the first ten characters is what this did before, and
    is never worse than raising halfway through a run."""
    assert cas.day_of("not-a-timestamp-at-all") == "not-a-time"


def test_a_naive_timestamp_is_read_as_utc():
    """What this project writes when an offset is missing."""
    assert cas.day_of("2026-09-21T22:30:00") == "2026-09-22"


def test_date_of_gives_a_date_or_none():
    assert cas.date_of("2026-09-21T22:30:00+00:00") == date(2026, 9, 22)
    assert cas.date_of("rubbish") is None


def test_to_prague_keeps_the_moment_and_changes_the_clock():
    moment = datetime(2026, 9, 21, 22, 30, tzinfo=timezone.utc)
    local = cas.to_prague(moment)
    assert local.hour == 0 and local.day == 22
    assert local == moment          # the same instant, differently written
