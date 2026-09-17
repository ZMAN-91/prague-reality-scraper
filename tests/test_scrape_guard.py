"""The guard that turns four attempts an hour into one run.

GitHub's schedule event is best effort and drops most of them, so the
workflow asks every fifteen minutes and throws away what it does not need.
Everything here is about the throwing-away being right, because both ways of
getting it wrong are bad and one of them is silent:

  - too eager, and the portals get four sweeps an hour instead of the one
    this project agreed to take;
  - too shy, and the collection stops and every attempt still reports
    success.

The fixtures are deliberately tiny - two or three runs - so a failure says
which rule broke rather than which fixture was misread.
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "tools" / "scrape_guard.sh"

SALE = ".github/workflows/scrape.yml"
RENT = ".github/workflows/scrape-rent.yml"
REPORT = ".github/workflows/report.yml"

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
SELF_ID = 999


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_row(run_id=1, path=SALE, status="completed", started_min_ago=120,
            duration_s=1600):
    """One row of the API's run list, by how long ago and how long it ran."""
    started = NOW - timedelta(minutes=started_min_ago)
    return {
        "id": run_id,
        "path": path,
        "status": status,
        "run_started_at": iso(started),
        "updated_at": iso(started + timedelta(seconds=duration_s)),
    }


def guard(rows, tmp_path, event="schedule", mine=None, **env):
    payload = tmp_path / "runs.json"
    payload.write_text(json.dumps({"workflow_runs": list(rows)}))
    argv = ["bash", str(GUARD), "--runs", str(payload)]
    if mine is not None:
        own = tmp_path / "mine.json"
        own.write_text(json.dumps({"workflow_runs": list(mine)}))
        argv += ["--mine", str(own)]
    result = subprocess.run(
        argv,
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "NOW": iso(NOW), "EVENT_NAME": event,
             "GITHUB_RUN_ID": str(SELF_ID), **{k: str(v) for k, v in env.items()}},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() in ("go=true", "go=false"), result.stdout
    return result.stdout.strip() == "go=true", result.stderr.strip()


def went(rows, tmp_path, **kw):
    return guard(rows, tmp_path, **kw)[0]


# --- nothing may overlap a sweep in flight -----------------------------------


def test_an_empty_history_runs(tmp_path):
    assert went([], tmp_path)


def test_a_sale_run_in_flight_stands_down(tmp_path):
    assert not went([run_row(1, SALE, status="in_progress")], tmp_path)


def test_a_rent_run_in_flight_stands_down(tmp_path):
    """Rent writes the same listings.csv. Two runs merging into it would
    race and one would lose its work."""
    assert not went([run_row(1, RENT, status="in_progress")], tmp_path)


def test_a_queued_run_counts_as_in_flight(tmp_path):
    """Waiting to start is not the same as not existing - without this the
    guard races the queue and both end up scraping."""
    assert not went([run_row(1, SALE, status="queued")], tmp_path)


def test_it_does_not_count_itself(tmp_path):
    """This attempt is in progress by definition. Counting itself would mean
    nothing ever ran again."""
    rows = [run_row(SELF_ID, SALE, status="in_progress")]
    assert went(rows, tmp_path)


def test_an_unrelated_workflow_is_not_a_reason_to_stop(tmp_path):
    """The weekly report touches REPORT.md, not the listing data."""
    assert went([run_row(1, REPORT, status="in_progress")], tmp_path)


# --- the floor: one sweep an hour, whatever the schedule delivers ------------


@pytest.mark.parametrize("minutes,expected", [
    (5, False), (30, False), (49, False),   # inside the floor
    (50, True), (75, True), (600, True),    # at it and past it
])
def test_the_floor_is_fifty_minutes(minutes, expected, tmp_path):
    rows = [run_row(1, started_min_ago=minutes)]
    assert went(rows, tmp_path) is expected


def test_the_floor_is_configurable(tmp_path):
    rows = [run_row(1, started_min_ago=20)]
    assert not went(rows, tmp_path, MIN_GAP_MINUTES=50)
    assert went(rows, tmp_path, MIN_GAP_MINUTES=15)


def test_the_most_recent_real_run_decides(tmp_path):
    """Not the first in the list, and not the oldest."""
    rows = [run_row(1, started_min_ago=600), run_row(2, started_min_ago=10)]
    assert not went(rows, tmp_path)


def test_the_api_order_does_not_matter(tmp_path):
    """The run list is not promised in any particular order."""
    rows = [run_row(1, started_min_ago=10), run_row(2, started_min_ago=600)]
    assert not went(rows, tmp_path)


# --- the silent killer -------------------------------------------------------


def test_a_run_the_guard_stopped_does_not_hold_the_floor(tmp_path):
    """THE bug in this design. A stopped attempt finishes in seconds and is
    still a completed run. Counted as "the last run" it holds the floor shut
    for another fifty minutes - and so does the next one, and the next, and
    the collection stops while every attempt still reports success."""
    rows = [run_row(1, started_min_ago=5, duration_s=14)]
    assert went(rows, tmp_path), "a 14-second run was never a sweep"


def test_a_wall_of_stopped_attempts_still_runs(tmp_path):
    """Four an hour for six hours, all stopped. The next one must still go."""
    rows = [run_row(i, started_min_ago=15 * i, duration_s=12) for i in range(1, 25)]
    assert went(rows, tmp_path)


def test_a_stopped_attempt_does_not_hide_the_real_one(tmp_path):
    """The other half: a recent stopped attempt must not make an old real
    run look like the last word when a newer real run exists."""
    rows = [run_row(1, started_min_ago=600, duration_s=1600),
            run_row(2, started_min_ago=20, duration_s=1600),
            run_row(3, started_min_ago=2, duration_s=11)]
    assert not went(rows, tmp_path), "the real run 20 minutes ago holds the floor"


def test_what_counts_as_real_is_configurable(tmp_path):
    rows = [run_row(1, started_min_ago=10, duration_s=200)]
    assert went(rows, tmp_path, MIN_REAL_RUN_SECONDS=300)
    assert not went(rows, tmp_path, MIN_REAL_RUN_SECONDS=100)


def test_a_crashed_run_does_not_hold_the_floor(tmp_path):
    """A run that died in checkout collected nothing, so the next attempt
    should not wait an hour on its behalf."""
    rows = [run_row(1, started_min_ago=5, duration_s=40)]
    assert went(rows, tmp_path)


# --- a person is not the schedule --------------------------------------------


def test_a_hand_triggered_run_ignores_the_floor(tmp_path):
    rows = [run_row(1, started_min_ago=1)]
    assert went(rows, tmp_path, event="workflow_dispatch")


def test_a_hand_triggered_run_does_not_even_read_the_history(tmp_path):
    """It short-circuits before the API call, which is why the scheduled
    path needs testing of its own - this one never exercises it."""
    ok, reason = guard([run_row(1, SALE, status="in_progress")], tmp_path,
                       event="workflow_dispatch")
    assert ok and "not the schedule" in reason


# --- malformed input must not stop the collection ----------------------------


def test_a_row_with_no_timestamps_is_skipped_not_fatal(tmp_path):
    rows = [{"id": 1, "path": SALE, "status": "completed",
             "run_started_at": None, "updated_at": None}]
    assert went(rows, tmp_path)


def test_a_broken_row_does_not_hide_a_good_one(tmp_path):
    rows = [{"id": 1, "path": SALE, "status": "completed",
             "run_started_at": None, "updated_at": None},
            run_row(2, started_min_ago=10)]
    assert not went(rows, tmp_path)


def test_the_reason_is_always_stated(tmp_path):
    """Whatever it decides, the log has to say why - this runs unattended
    ninety-six times a day and nobody watches it."""
    for rows in ([], [run_row(1, SALE, status="in_progress")],
                 [run_row(1, started_min_ago=5)], [run_row(1, started_min_ago=90)]):
        _, reason = guard(rows, tmp_path)
        assert len(reason) > 20, reason


# --- the weekly pass, which measures itself ---------------------------------


def test_the_rent_pass_is_not_held_back_by_a_sale_sweep(tmp_path):
    """The whole reason MEASURE_WORKFLOW exists. Rent shares the repository
    with a sweep that ran minutes ago; measured against that it would never
    run at all."""
    rows = [run_row(1, SALE, started_min_ago=5),
            run_row(2, RENT, started_min_ago=60 * 24 * 7)]
    assert went(rows, tmp_path, MEASURE_WORKFLOW=RENT.split("/")[-1],
                MIN_GAP_MINUTES=8640)


def test_the_rent_pass_runs_once_a_week_not_once_a_window(tmp_path):
    """Sixteen attempts arrive across the Sunday window. The first may start
    a four-hour city-wide pass; the fifteen behind it may not."""
    rows = [run_row(1, RENT, started_min_ago=30, duration_s=3600)]
    assert not went(rows, tmp_path, MEASURE_WORKFLOW=RENT.split("/")[-1],
                    MIN_GAP_MINUTES=8640)


def test_a_week_later_the_rent_pass_runs_again(tmp_path):
    rows = [run_row(1, RENT, started_min_ago=60 * 24 * 7, duration_s=3600)]
    assert went(rows, tmp_path, MEASURE_WORKFLOW=RENT.split("/")[-1],
                MIN_GAP_MINUTES=8640)


def test_a_sale_sweep_in_flight_still_stops_the_rent_pass(tmp_path):
    """Measuring separately must not mean ignoring each other: they write the
    same listings.csv, so either running is a reason to stand down."""
    rows = [run_row(1, SALE, status="in_progress"),
            run_row(2, RENT, started_min_ago=60 * 24 * 7)]
    assert not went(rows, tmp_path, MEASURE_WORKFLOW=RENT.split("/")[-1],
                    MIN_GAP_MINUTES=8640)


def test_the_sale_guard_still_ignores_rent_history_for_its_floor(tmp_path):
    """The mirror image: a rent pass an hour ago is not a sale sweep, so it
    must not hold the hourly floor shut."""
    rows = [run_row(1, RENT, started_min_ago=60, duration_s=3600)]
    assert went(rows, tmp_path)


def test_the_default_workflow_is_the_hourly_one(tmp_path):
    """Nothing passes MEASURE_WORKFLOW for the sale workflow, so the default
    has to be right or the hourly floor silently stops working."""
    rows = [run_row(1, SALE, started_min_ago=5)]
    assert not went(rows, tmp_path)


def test_the_floor_reads_this_workflow_s_own_history(tmp_path):
    """The repository-wide list holds about one day, because the waker puts
    ninety-six runs a day into it. The weekly floor has to look further back
    than that, so it asks a different endpoint - and if it ever stopped
    doing so, rent would find no previous pass and start at every attempt.

    Here the two disagree on purpose: repository-wide shows nothing, the
    workflow's own history shows a pass thirty minutes ago.
    """
    assert not went([], tmp_path,
                    mine=[run_row(1, RENT, started_min_ago=30, duration_s=3600)],
                    MEASURE_WORKFLOW="scrape-rent.yml", MIN_GAP_MINUTES=8640)


def test_the_busy_check_reads_the_repository_wide_list(tmp_path):
    """The mirror: a sale sweep in flight appears repository-wide, never in
    rent's own history, and must still stop the pass."""
    assert not went([run_row(1, SALE, status="in_progress")], tmp_path,
                    mine=[], MEASURE_WORKFLOW="scrape-rent.yml",
                    MIN_GAP_MINUTES=8640)
