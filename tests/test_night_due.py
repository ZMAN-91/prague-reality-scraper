"""Letting exactly one city-wide pass through per night.

The hourly pass walks two districts. Everything outside them, and every
absence, rests on one city-wide pass a day - so this decides whether a day's
coverage happens at all.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from tools import night_due


def logs(tmp_path, entries):
    directory = tmp_path / "logs"
    directory.mkdir(exist_ok=True)
    with open(directory / "2026-09-21.jsonl", "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return directory


def run(started_at, scope="city"):
    return {"started_at": started_at, "scope": scope, "sources": {}}


TODAY = date(2026, 9, 21)


def test_nothing_logged_at_all_is_due(tmp_path):
    go, why = night_due.due(logs(tmp_path, []), today=TODAY)
    assert go is True
    assert "ever been logged" in why


def test_a_city_pass_today_is_not_due(tmp_path):
    directory = logs(tmp_path, [run("2026-09-21T00:30:00+00:00")])
    go, why = night_due.due(directory, today=TODAY)
    assert go is False
    assert "already ran today" in why


def test_a_city_pass_yesterday_is_due(tmp_path):
    directory = logs(tmp_path, [run("2026-09-20T00:30:00+00:00")])
    go, _ = night_due.due(directory, today=TODAY)
    assert go is True


def test_a_day_of_area_passes_does_not_count(tmp_path):
    """The failure this check exists to prevent. Twenty area passes cover two
    districts; read as "today is done", the rest of Prague would go unseen
    and nothing anywhere would say so."""
    directory = logs(tmp_path, [run(f"2026-09-21T{hour:02d}:16:00+00:00", scope="area")
                                for hour in range(6, 20)])
    go, why = night_due.due(directory, today=TODAY)
    assert go is True
    assert "ever been logged" in why


def test_the_day_is_prague_s(tmp_path):
    """A pass at 23:30 UTC on the 20th is 01:30 on the 21st here, and is
    today's pass - not yesterday's."""
    directory = logs(tmp_path, [run("2026-09-20T23:30:00+00:00")])
    go, _ = night_due.due(directory, today=TODAY)
    assert go is False


def test_runs_from_before_scopes_existed_count_as_city(tmp_path):
    """They had no scope field and they did walk the whole city. Treating
    them as area passes would run a duplicate on the first night."""
    directory = logs(tmp_path, [{"started_at": "2026-09-21T00:30:00+00:00",
                                 "sources": {}}])
    go, _ = night_due.due(directory, today=TODAY)
    assert go is False


def test_the_newest_city_pass_wins(tmp_path):
    directory = logs(tmp_path, [run("2026-09-19T00:30:00+00:00"),
                                run("2026-09-21T00:30:00+00:00"),
                                run("2026-09-20T00:30:00+00:00")])
    assert night_due.due(directory, today=TODAY)[0] is False


def test_a_corrupt_line_does_not_stop_the_night(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    with open(directory / "2026-09-21.jsonl", "w", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write(json.dumps(run("2026-09-21T00:30:00+00:00")) + "\n")
    assert night_due.due(directory, today=TODAY)[0] is False


def test_an_unreadable_log_is_due_rather_than_skipped(tmp_path, monkeypatch):
    """A missed night costs a day of coverage outside the watched districts;
    a duplicate costs one extra walk. Those are not the same size of mistake,
    so anything unreadable means due.

    The first version of this test monkeypatched `open` and asserted due=True
    - which it got from the empty-log branch whether the handling existed or
    not. It now checks the reason, which only the unreadable path produces
    while a genuine pass is on disk.
    """
    directory = logs(tmp_path, [run("2026-09-21T00:30:00+00:00")])
    real_open = open

    def boom(path, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            raise OSError("disk went away")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", boom)
    go, why = night_due.due(directory, today=TODAY)
    assert go is True
    assert "could not be read" in why


def test_cli_prints_the_github_output_line(tmp_path, capsys, monkeypatch):
    directory = logs(tmp_path, [])
    monkeypatch.setattr("sys.argv", ["night_due", "--logs-dir", str(directory)])
    assert night_due.main() == 0
    assert "go=true" in capsys.readouterr().out
