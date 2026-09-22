"""The heartbeat that keeps the scheduler alive, and used to depend on it.

GitHub disables scheduled workflows in a public repository after 60 days
without activity. Every commit this collector makes goes to the private data
repository, so the heartbeat is the only thing pushing here - and it asked
for one attempt a week from a scheduler that delivered 1 of 32.
"""

from __future__ import annotations

from datetime import date

from tools import heartbeat_due

STATUS = ("# Stav\n\nSběr běží. Poslední heartbeat: **{}** 04:17 UTC.\n")


def test_a_fresh_heartbeat_is_not_due():
    due, _ = heartbeat_due.is_due(STATUS.format("2026-09-21"),
                                  date(2026, 9, 21))
    assert not due


def test_a_week_old_heartbeat_is_due():
    due, why = heartbeat_due.is_due(STATUS.format("2026-09-14"),
                                    date(2026, 9, 21))
    assert due and "7 days ago" in why


def test_the_days_between_are_not_due():
    """Four attempts a day must produce one commit a week, not twenty-eight."""
    for day in range(1, heartbeat_due.MAX_AGE_DAYS):
        due, _ = heartbeat_due.is_due(
            STATUS.format("2026-09-14"),
            date(2026, 9, 14) + __import__("datetime").timedelta(days=day))
        assert not due, day


def test_no_status_file_at_all_is_due():
    due, why = heartbeat_due.is_due(None, date(2026, 9, 21))
    assert due and "no heartbeat" in why


def test_a_status_file_without_a_stamp_is_due():
    """Better a redundant commit than a silent stop: the failure this guards
    against is the whole collection switching off with nothing red."""
    due, _ = heartbeat_due.is_due("# Stav\n\nSběr běží.\n", date(2026, 9, 21))
    assert due


def test_an_unparseable_date_is_due():
    due, _ = heartbeat_due.is_due(
        "Poslední heartbeat: **2026-13-45** 04:17 UTC", date(2026, 9, 21))
    assert due


def test_a_stamp_in_the_future_is_due():
    """A clock that ran backwards must not freeze the heartbeat for ever."""
    due, why = heartbeat_due.is_due(STATUS.format("2027-01-01"),
                                    date(2026, 9, 21))
    assert due and "future" in why


def test_the_cadence_stays_far_inside_githubs_window():
    """Sixty days is the limit. A heartbeat every seven, from four attempts a
    day, leaves the collection a long way from being switched off."""
    assert heartbeat_due.MAX_AGE_DAYS <= 14


def test_the_command_prints_what_a_workflow_step_reads(tmp_path, capsys):
    path = tmp_path / "STATUS.md"
    assert heartbeat_due.main(["--status", str(path),
                               "--today", "2026-09-21"]) == 0
    assert "go=true" in capsys.readouterr().out

    path.write_text(STATUS.format("2026-09-21"), encoding="utf-8")
    assert heartbeat_due.main(["--status", str(path),
                               "--today", "2026-09-21"]) == 0
    assert "go=false" in capsys.readouterr().out
