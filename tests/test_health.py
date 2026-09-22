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


def test_rent_that_never_ran_at_all_is_reported(tmp_path):
    """The silence this check actually had to break.

    Rent was stopped by its own guard every Sunday and produced nothing for
    days. `of_kind(runs, "pronajem")` was empty, the check returned nothing,
    and a pass that had never happened read exactly like a healthy one.
    """
    runs = [run(minutes_ago=60 * 24 * 12), run(minutes_ago=30)]
    got = findings(tmp_path, runs)
    assert any("never run" in f for f in got), got


def test_rent_missing_from_a_young_dataset_is_not_reported(tmp_path):
    """Two days in, a weekly pass is not yet owed."""
    runs = [run(minutes_ago=60 * 24 * 2), run(minutes_ago=30)]
    assert findings(tmp_path, runs) == []


# --- what the newest run says about itself -----------------------------------


def test_errors_are_surfaced(tmp_path):
    sources = {"idnes": {"fetched": 10, "errors": ["HTTP 500"], "run_complete": True}}
    got = findings(tmp_path, [run(sources=sources)])
    assert any("HTTP 500" in f for f in got), got


def test_one_unfinished_sweep_is_not_a_fault(tmp_path):
    """This test used to assert the opposite, and the opposite cost nine
    failed jobs and nine emails in nine hours.

    Not finishing in one run is the designed state for two of the three
    sources. iDNES rotates through ~257 index pages, so an hourly run sees
    a tenth of Prague; sreality's hourly pass walks two boroughs on purpose
    and must not claim otherwise - that claim is the bug that once marked
    1,201 live listings missing. Calling either a failure fires every hour
    for ever, which is what this file's own docstring warns against.
    """
    got = findings(tmp_path, [
        run(minutes_ago=90, sources={
            "idnes": {"fetched": 10, "errors": [], "run_complete": True}}),
        run(minutes_ago=30, sources={
            "idnes": {"fetched": 10, "errors": [], "run_complete": False}}),
    ], days=["2026-09-16", "2026-09-17"])
    assert got == [], got


def test_a_source_that_has_not_completed_a_sweep_in_over_a_day_is_surfaced(tmp_path):
    """The condition that IS worth an email: removals have genuinely stopped
    being recorded. Every source completes a sweep at least daily."""
    got = findings(tmp_path, [
        run(minutes_ago=60 * 40, sources={
            "idnes": {"fetched": 10, "errors": [], "run_complete": True}}),
        run(minutes_ago=30, sources={
            "idnes": {"fetched": 10, "errors": [], "run_complete": False}}),
    ])
    assert any("has not completed a sweep" in f for f in got), got


def test_a_source_that_never_completed_one_is_surfaced(tmp_path):
    got = findings(tmp_path, [run(sources={
        "idnes": {"fetched": 10, "errors": [], "run_complete": False}})])
    assert any("never completed a sweep" in f for f in got), got


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


# --- the hours sale is not supposed to be running in -------------------------

SUNDAY_0500 = datetime(2026, 9, 20, 5, 30, tzinfo=timezone.utc)


def gap_findings(last_sweep, now):
    runs = [{"started_at": last_sweep.isoformat(), "transactions": ["prodej"],
             "sources": {}}]
    return health.check_gap(runs, now)


def test_the_sunday_rent_window_is_not_a_broken_waker(tmp_path):
    """The real Sunday: last sale sweep 01:24, next attempt 05:00.

    Three and a half hours of nothing, all of it inside the window the sale
    cron skips and the waker sleeps through. Reported as a fault it would
    have fired every Sunday morning for as long as the project runs.
    """
    last = datetime(2026, 9, 20, 1, 24, tzinfo=timezone.utc)
    assert gap_findings(last, SUNDAY_0500) == []


def test_a_waker_that_dies_during_the_rent_window_is_still_caught(tmp_path):
    """The window excuses its own five hours and not one minute more."""
    last = datetime(2026, 9, 20, 1, 24, tzinfo=timezone.utc)
    # Four hours past the window's close, so four hours unaccounted for.
    got = gap_findings(last, datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc))
    assert any("not waking it" in f for f in got), got


def test_a_saturday_gap_gets_no_such_excuse(tmp_path):
    """Same hours, wrong day."""
    last = datetime(2026, 9, 19, 1, 24, tzinfo=timezone.utc)
    got = gap_findings(last, datetime(2026, 9, 19, 5, 30, tzinfo=timezone.utc))
    assert any("not waking it" in f for f in got), got


def test_a_gap_spanning_the_whole_weekend_counts_the_window_once(tmp_path):
    """Friday to Sunday morning is two days of silence, not two days minus
    a window per calendar day touched."""
    last = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
    got = gap_findings(last, SUNDAY_0500)
    assert any("not waking it" in f for f in got), got
    hours = float(got[0].split()[3])
    assert 36 < hours < 42, got


def test_one_bad_category_does_not_erase_the_whole_source(tmp_path):
    """Found by an end-to-end run, not by reasoning.

    run_complete is `bool(scopes) and not real_errors`, so a suspicious drop
    in ONE category appends an error and the whole source reads incomplete -
    even though the other three finished cleanly. sreality's dum/pronajem is
    65 active adverts: small enough to be volatile, big enough to trip the
    drop guard. Thirty hours later this would start emailing about a sweep
    that happened.
    """
    got = findings(tmp_path, [
        run(minutes_ago=90, sources={"sreality": {
            "fetched": 8000, "errors": [], "run_complete": True,
            "scopes_absence_marked": ["byt/prodej", "byt/pronajem",
                                      "dum/prodej", "dum/pronajem"]}}),
        run(minutes_ago=30, sources={"sreality": {
            "fetched": 8000,
            "errors": ["suspicious drop in active listings for sreality "
                       "dum/pronajem (65 -> 0); skipping absence-marking"],
            "run_complete": False,
            "scopes_absence_marked": ["byt/prodej", "byt/pronajem",
                                      "dum/prodej"]}}),
    ], days=["2026-09-16", "2026-09-17"])
    assert not any("has not completed" in f or "never completed" in f
                   for f in got), got


def test_a_category_that_really_has_gone_quiet_is_still_surfaced(tmp_path):
    """The other side: per-scope must not mean per-scope-is-optional. One
    category that has not been absence-marked for over a day is a category
    whose removals stopped being recorded."""
    got = findings(tmp_path, [
        run(minutes_ago=60 * 40, sources={"sreality": {
            "fetched": 8000, "errors": [], "run_complete": True,
            "scopes_absence_marked": ["byt/prodej", "dum/pronajem"]}}),
        run(minutes_ago=30, sources={"sreality": {
            "fetched": 8000, "errors": [], "run_complete": True,
            "scopes_absence_marked": ["byt/prodej"]}}),
    ])
    assert any("dum/pronajem has not completed" in f for f in got), got
    assert not any("byt/prodej has not completed" in f for f in got), got


def test_a_pass_that_completes_no_scope_does_not_invent_a_permanent_finding(tmp_path):
    """The bug the first version of this fix shipped with, caught by an
    end-to-end run and not by any unit test here.

    The hourly area pass completes no scope by design. Registering an entry
    for it produced "sreality has never completed a sweep" on every run for
    ever - an empty entry that nothing could ever fill. That is precisely
    the failure this whole check was rewritten to stop, reintroduced one
    level down.
    """
    got = findings(tmp_path, [
        run(minutes_ago=90, sources={"sreality": {
            "fetched": 8000, "errors": [], "run_complete": True,
            "scopes_absence_marked": ["byt/prodej"]}}),
        run(minutes_ago=30, sources={"sreality": {
            "fetched": 2900, "errors": [], "run_complete": False,
            "scopes_absence_marked": []}}),
    ], days=["2026-09-16", "2026-09-17"])
    assert got == [], got


def test_a_source_that_has_completed_nothing_at_all_is_still_surfaced(tmp_path):
    """The one case the per-scope view cannot see on its own: with no
    completed scope there is no scope to report on. Broken from day one has
    to be louder than merely partial."""
    got = findings(tmp_path, [
        run(minutes_ago=90, sources={"sreality": {
            "fetched": 2900, "errors": [], "run_complete": False,
            "scopes_absence_marked": []}}),
        run(minutes_ago=30, sources={"sreality": {
            "fetched": 2900, "errors": [], "run_complete": False,
            "scopes_absence_marked": []}}),
    ], days=["2026-09-16", "2026-09-17"])
    assert any("never completed a sweep" in f for f in got), got
