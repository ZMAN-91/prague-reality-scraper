"""Letting one report through per week, including on the retry day.

Attempts arrive across a window on Monday and again on Tuesday when Monday
produced nothing.
"""

from datetime import date
from pathlib import Path

import pytest

from tools import report_due

MONDAY = date(2026, 9, 21)      # the week to Sunday 2026-09-20 is 2026-W38
TUESDAY = date(2026, 9, 22)


def with_report(tmp_path, week, text="# Report trhu\n"):
    directory = tmp_path / "reports"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{week}.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_no_report_for_the_closed_week_is_due(tmp_path):
    go, why = report_due.due(tmp_path, today=MONDAY)
    assert go is True
    assert "2026-W38" in why


def test_the_weeks_report_already_written_is_not_due(tmp_path):
    go, why = report_due.due(with_report(tmp_path, "2026-W38"), today=MONDAY)
    assert go is False


def test_tuesday_finds_mondays_work(tmp_path):
    """The retry must not produce a second report for the same week."""
    root = with_report(tmp_path, "2026-W38")
    assert report_due.due(root, today=TUESDAY)[0] is False


def test_tuesday_does_the_work_when_monday_produced_nothing(tmp_path):
    """And must still do it. An age-based check cannot tell 'yesterday
    succeeded' from 'yesterday is the failure I am retrying'."""
    go, why = report_due.due(tmp_path, today=TUESDAY)
    assert go is True
    assert "2026-W38" in why


def test_last_weeks_report_does_not_satisfy_this_week(tmp_path):
    root = with_report(tmp_path, "2026-W37")
    assert report_due.due(root, today=MONDAY)[0] is True


def test_an_empty_file_is_not_a_report(tmp_path):
    """A run that died mid-write leaves the name behind. Treating that as
    done loses the week for good - the series moves on."""
    root = with_report(tmp_path, "2026-W38", text="")
    assert report_due.due(root, today=MONDAY)[0] is True


def test_the_week_is_the_one_that_ended_not_the_one_running(tmp_path):
    """Monday starts a new ISO week. Asking for it would look for a report on
    a week with six days still to happen, and write one every Monday for
    ever."""
    root = with_report(tmp_path, "2026-W39")
    assert report_due.due(root, today=MONDAY)[0] is True


def test_something_unreadable_is_due_rather_than_skipped(tmp_path, monkeypatch):
    root = with_report(tmp_path, "2026-W38")

    def boom(self):
        raise OSError("disk went away")

    monkeypatch.setattr(Path, "is_file", boom)
    go, why = report_due.due(root, today=MONDAY)
    assert go is True
    assert "Could not look" in why


def test_cli_prints_the_github_output_line(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("sys.argv", ["report_due", "--data-dir", str(data)])
    assert report_due.main() == 0
    assert "go=true" in capsys.readouterr().out
