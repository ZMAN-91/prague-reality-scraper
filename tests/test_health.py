"""The check that notices the collection has stopped.

Every finding here is one this project has actually had, or one its design
makes possible. The tests matter in both directions: a check that never fires
is decoration, and one that fires when nothing is wrong trains you to ignore
the whole file.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from tools import health

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)


def run(minutes_ago=30, transaction="prodej", sources=None, **extra):
    started = NOW - timedelta(minutes=minutes_ago)
    row = {
        "started_at": started.isoformat(),
        "transactions": [transaction],
        "sources": sources if sources is not None else {
            "sreality": {"fetched": 8000, "errors": [], "run_complete": True},
        },
    }
    row.update(extra)
    return row


def write(tmp_path, runs, days=()):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / "2026-09-17.jsonl", "w", encoding="utf-8") as handle:
        for row in runs:
            handle.write(json.dumps(row) + "\n")
    data = tmp_path / "data" / "csv"
    data.mkdir(parents=True, exist_ok=True)
    if days:
        with open(data / "trh_denne.csv", "w", encoding="utf-8") as handle:
            handle.write("den,segment\n")
            for day in days:
                handle.write(f"{day},vse/prodej\n")
    return tmp_path / "data", logs


def findings(tmp_path, runs, days=()):
    data, logs = write(tmp_path, runs, days)
    return health.report(data, logs, now=NOW)


# --- the healthy case, which must stay quiet ---------------------------------


def test_a_healthy_collection_says_nothing(tmp_path):
    runs = [run(minutes_ago=180), run(minutes_ago=120), run(minutes_ago=30)]
    assert findings(tmp_path, runs, days=["2026-09-16", "2026-09-17"]) == []


def test_an_empty_log_is_reported_once_not_as_five_problems(tmp_path):
    assert len(findings(tmp_path, [])) == 1


# --- silence: the failure mode that has actually happened --------------------


def test_a_long_silence_is_reported(tmp_path):
    """GitHub dropped every scheduled attempt for nine hours and the only
    symptom was that nothing happened."""
    got = findings(tmp_path, [run(minutes_ago=60 * 9)])
    assert any("Nothing collected for 9.0 hours" in f for f in got), got


def test_an_ordinary_hourly_gap_is_not_reported(tmp_path):
    assert findings(tmp_path, [run(minutes_ago=65)]) == []


@pytest.mark.parametrize("hours,reported", [(2.5, False), (3.5, True)])
def test_the_silence_threshold(hours, reported, tmp_path):
    got = findings(tmp_path, [run(minutes_ago=hours * 60)])
    assert bool(got) is reported


# --- the guard failing open: the portals pay for this ------------------------


def test_two_sweeps_too_close_together_are_reported(tmp_path):
    """If the guard stops rationing, the waker's four firings an hour each
    become a sweep."""
    runs = [run(minutes_ago=45), run(minutes_ago=30)]
    got = findings(tmp_path, runs)
    assert any("15 minutes apart" in f for f in got), got


def test_sweeps_an_hour_apart_are_not_reported(tmp_path):
    assert findings(tmp_path, [run(minutes_ago=90), run(minutes_ago=30)]) == []


def test_the_rent_pass_does_not_count_as_a_sale_sweep(tmp_path):
    """They measure separately everywhere else; here too, or a Sunday rent
    pass would read as the guard failing."""
    runs = [run(minutes_ago=40, transaction="pronajem"), run(minutes_ago=30)]
    assert findings(tmp_path, runs) == []


# --- the weekly pass, which fails quietly by definition ----------------------


def test_a_missed_rent_week_is_reported(tmp_path):
    runs = [run(minutes_ago=60 * 24 * 9, transaction="pronajem"), run(minutes_ago=30)]
    got = findings(tmp_path, runs)
    assert any("rent pass has not run" in f for f in got), got


def test_a_rent_pass_within_the_week_is_not_reported(tmp_path):
    runs = [run(minutes_ago=60 * 24 * 6, transaction="pronajem"), run(minutes_ago=30)]
    assert findings(tmp_path, runs) == []


# --- what the newest run says about itself -----------------------------------


def test_errors_are_surfaced(tmp_path):
    sources = {"idnes": {"fetched": 10, "errors": ["HTTP 500"], "run_complete": True}}
    got = findings(tmp_path, [run(sources=sources)])
    assert any("HTTP 500" in f for f in got), got


def test_an_unfinished_sweep_is_surfaced(tmp_path):
    """It decides whether absence may be read as removal, so a sweep that
    did not finish is not a smaller version of one that did."""
    sources = {"idnes": {"fetched": 10, "errors": [], "run_complete": False}}
    got = findings(tmp_path, [run(sources=sources)])
    assert any("did not finish" in f for f in got), got


def test_a_source_that_fetched_nothing_is_surfaced(tmp_path):
    """Zero is what a changed portal or a block looks like, and the run is
    green either way."""
    sources = {"bezrealitky": {"fetched": 0, "errors": [], "run_complete": True}}
    got = findings(tmp_path, [run(sources=sources)])
    assert any("fetched nothing" in f for f in got), got


# --- the series itself -------------------------------------------------------


def test_a_missing_day_in_the_series_is_reported(tmp_path):
    """The definitive record of whether anything was collected that day."""
    got = findings(tmp_path, [run()],
                   days=["2026-09-14", "2026-09-15", "2026-09-17"])
    assert any("2026-09-16" in f for f in got), got


def test_a_complete_series_is_not_reported(tmp_path):
    days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17"]
    assert findings(tmp_path, [run()], days=days) == []


def test_a_one_day_series_is_not_a_gap(tmp_path):
    assert findings(tmp_path, [run()], days=["2026-09-17"]) == []


# --- malformed input must not silence the check ------------------------------


def test_a_broken_line_does_not_stop_the_rest(tmp_path):
    data, logs = write(tmp_path, [run(minutes_ago=30)])
    with open(logs / "2026-09-17.jsonl", "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    assert health.report(data, logs, now=NOW) == []


def test_an_unparseable_timestamp_does_not_crash(tmp_path):
    assert health.report(*write(tmp_path, [run(started_at="nonsense")]),
                         now=NOW) is not None
