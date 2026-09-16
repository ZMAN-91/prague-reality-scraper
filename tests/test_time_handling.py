"""Timestamp parsing and granularity.

These look trivial but hide a real trap: `first_seen_at` is a full
timezone-aware ISO timestamp while `last_seen_at` is a bare date, and
Python raises TypeError when you compare a naive datetime with an aware one.
Re-listing detection compares exactly those two fields, so a naive parse
would crash the pass - silently, inside a broad except, in the worst case.
"""

from datetime import datetime, timezone

import pytest

from common.dedup import _on_market_window, _windows_overlap
from common.schema import day_of, parse_iso


def test_parse_iso_handles_full_timestamp():
    parsed = parse_iso("2026-03-01T10:30:00+00:00")
    assert parsed == datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)


def test_parse_iso_handles_bare_date_as_utc_midnight():
    parsed = parse_iso("2026-03-01")
    assert parsed == datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)


def test_parse_iso_result_is_always_aware():
    """The whole point: mixing naive and aware datetimes raises TypeError."""
    for value in ("2026-03-01", "2026-03-01T10:30:00+00:00", "2026-03-01T10:30:00"):
        assert parse_iso(value).tzinfo is not None


def test_mixed_granularity_values_are_directly_comparable():
    day = parse_iso("2026-03-01")
    stamp = parse_iso("2026-03-01T10:30:00+00:00")
    assert day < stamp  # would raise TypeError if either were naive


def test_parse_iso_returns_none_for_junk_instead_of_raising():
    """One malformed historical row must never crash a run."""
    for value in ("", None, "not a date", "2026-13-45", "???"):
        assert parse_iso(value) is None


def test_day_of_truncates_to_the_date():
    assert day_of("2026-03-01T23:59:59+00:00") == "2026-03-01"
    assert day_of("2026-03-01") == "2026-03-01"


# --- on-market windows ---------------------------------------------------


def row(first_seen, last_seen):
    return {"first_seen_at": first_seen, "last_seen_at": last_seen}


def test_window_end_is_never_before_its_start():
    """A listing first seen at 14:03 today has last_seen_at "today", which
    parses to midnight - i.e. earlier than its own start."""
    window = _on_market_window(row("2026-03-01T14:03:00+00:00", "2026-03-01"))
    assert window[0] <= window[1]


def test_overlapping_windows_overlap():
    a = row("2026-03-01T00:00:00+00:00", "2026-03-20")
    b = row("2026-03-10T00:00:00+00:00", "2026-03-30")
    assert _windows_overlap(a, b) is True


def test_windows_separated_by_less_than_the_tolerance_still_overlap():
    a = row("2026-01-01T00:00:00+00:00", "2026-01-31")
    b = row("2026-02-20T00:00:00+00:00", "2026-03-10")  # 20 days after a ended
    assert _windows_overlap(a, b) is True


def test_windows_years_apart_do_not_overlap():
    a = row("2023-01-01T00:00:00+00:00", "2023-02-01")
    b = row("2026-01-01T00:00:00+00:00", "2026-02-01")
    assert _windows_overlap(a, b) is False


def test_unknown_timestamps_do_not_veto_a_match():
    """An unparseable window must not silently block an otherwise strong
    GPS+area+disposition match."""
    a = row("", "")
    b = row("2026-03-01T00:00:00+00:00", "2026-03-10")
    assert _windows_overlap(a, b) is True


@pytest.mark.parametrize("tolerance", [0, 1, 30, 365])
def test_overlap_is_symmetric(tolerance):
    a = row("2026-01-01T00:00:00+00:00", "2026-02-01")
    b = row("2026-03-01T00:00:00+00:00", "2026-04-01")
    assert _windows_overlap(a, b, tolerance) == _windows_overlap(b, a, tolerance)
