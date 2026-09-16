"""Continuing where the last run stopped.

The property that matters: over several short runs, every item in the queue
gets visited exactly once per cycle, with none skipped and none repeated.
That is what makes "if it does not finish in an hour, carry on next hour"
a guarantee rather than a hope.
"""

import json

from common import progress


def test_missing_file_reads_as_a_fresh_start(tmp_path):
    assert progress.read(tmp_path) == {}
    assert progress.cursor_of({}, "bezrealitky") == 0
    assert progress.cycle_of({}, "bezrealitky") == 0


def test_a_corrupt_file_does_not_stop_a_run(tmp_path):
    """One malformed byte in a bookkeeping file must never be the reason a
    year-old unattended scraper stops working."""
    (tmp_path / progress.PROGRESS_FILENAME).write_text("{not json", encoding="utf-8")
    assert progress.read(tmp_path) == {}


def test_progress_survives_a_write_and_read(tmp_path):
    state = progress.advance({}, "idnes", consumed=40, queue_length=100)
    progress.write(tmp_path, state)
    assert progress.read(tmp_path) == state
    assert progress.cursor_of(progress.read(tmp_path), "idnes") == 40


def test_the_file_is_readable_by_a_person(tmp_path):
    progress.write(tmp_path, progress.advance({}, "idnes", 5, 10))
    text = (tmp_path / progress.PROGRESS_FILENAME).read_text(encoding="utf-8")
    assert "\n" in text and json.loads(text)["idnes"]["cursor"] == 5


def test_cursor_wraps_and_counts_completed_cycles():
    state = progress.advance({}, "bezrealitky", consumed=700, queue_length=1000)
    assert (progress.cursor_of(state, "bezrealitky"), progress.cycle_of(state, "bezrealitky")) == (700, 0)
    state = progress.advance(state, "bezrealitky", consumed=700, queue_length=1000)
    assert (progress.cursor_of(state, "bezrealitky"), progress.cycle_of(state, "bezrealitky")) == (400, 1)


def test_a_run_longer_than_the_whole_queue_counts_every_lap():
    state = progress.advance({}, "x", consumed=2500, queue_length=1000)
    assert progress.cycle_of(state, "x") == 2
    assert progress.cursor_of(state, "x") == 500


def test_rotate_starts_at_the_cursor_and_wraps():
    items = ["a", "b", "c", "d", "e"]
    assert progress.rotate(items, 0) == items
    assert progress.rotate(items, 2) == ["c", "d", "e", "a", "b"]
    assert progress.rotate(items, 7) == ["c", "d", "e", "a", "b"]  # cursor past the end
    assert progress.rotate([], 3) == []


def test_several_short_runs_cover_the_queue_exactly_once():
    """The whole point, as a property test: 10 runs of 100 items over a queue
    of 1000 must visit each item once - no gaps, no repeats."""
    queue = [f"item-{n:04d}" for n in range(1000)]
    state: dict = {}
    visited = []
    for _ in range(10):
        batch = progress.rotate(queue, progress.cursor_of(state, "s"))[:100]
        visited.extend(batch)
        state = progress.advance(state, "s", consumed=len(batch), queue_length=len(queue))
    assert len(visited) == 1000
    assert sorted(visited) == sorted(queue)
    assert progress.cycle_of(state, "s") == 1


def test_an_empty_queue_leaves_the_cursor_alone():
    state = progress.advance({"s": {"cursor": 17, "cycle": 2}}, "s", consumed=0, queue_length=0)
    assert progress.cursor_of(state, "s") == 17
    assert progress.cycle_of(state, "s") == 2


def test_note_records_whatever_a_source_needs_to_remember():
    state = progress.note({}, "idnes", last_page=12, pages_total=257)
    assert state["idnes"]["last_page"] == 12
    state = progress.note(state, "idnes", last_page=24)
    assert state["idnes"]["last_page"] == 24 and state["idnes"]["pages_total"] == 257


# --- the weekly reset -----------------------------------------------------


def _fetch_idnes_with(progress, when, monkeypatch, completed=()):
    """Run fetch_idnes against a stubbed scraper.

    Returns the state stored under "idnes" plus the keyword arguments the
    scraper was called with, which is where the cursor handed forward from
    the previous run shows up.
    """
    from common.budget import Budget
    from scrapers import idnes

    seen = {}

    def stub(*args, **kwargs):
        seen.update(kwargs)
        return [], [], [], set(completed), 0, {"byt/prodej": 7}

    monkeypatch.setattr(idnes, "fetch_all", stub)
    import run as run_module

    run_module.fetch_idnes(None, {}, Budget(max_seconds=1, max_new_details=1),
                           when, ["prodej"], progress)
    return progress["idnes"], seen


def test_an_unfinished_sweep_carries_on_within_the_week(monkeypatch):
    """The ordinary case: an hour ran out mid-index. The next run picks the
    cursor back up, and the sweep still counts as the one that began on
    Tuesday - otherwise every run would look like a fresh pass and nothing
    could ever be judged absent."""
    from datetime import datetime, timezone

    tuesday = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
    wednesday = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)

    progress = {}
    _fetch_idnes_with(progress, tuesday, monkeypatch)
    entry, called = _fetch_idnes_with(progress, wednesday, monkeypatch)

    assert called["page_cursor"] == {"byt/prodej": 7}, "where the last run stopped"
    assert entry["week"] == "2026-W38"
    assert entry["sweep_started"]["byt/prodej"] == "2026-09-15", "the sweep window must survive"


def test_a_finished_sweep_opens_the_next_one(monkeypatch):
    """A scope that completed its pass is judged against the day that pass
    began, and then starts a new window from today."""
    from datetime import datetime, timezone

    tuesday = datetime(2026, 9, 15, 10, tzinfo=timezone.utc)
    wednesday = datetime(2026, 9, 16, 10, tzinfo=timezone.utc)

    progress = {}
    _fetch_idnes_with(progress, tuesday, monkeypatch)
    entry, _ = _fetch_idnes_with(progress, wednesday, monkeypatch,
                                 completed=[("byt", "prodej")])
    assert entry["sweep_started"]["byt/prodej"] == "2026-09-16"


def test_monday_starts_the_sweep_over(monkeypatch):
    """A week that did not manage a complete pass simply ends; the next one
    begins from the front rather than from wherever the last one stopped."""
    from datetime import datetime, timezone

    saturday = datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
    monday = datetime(2026, 9, 21, 10, tzinfo=timezone.utc)

    progress = {}
    _fetch_idnes_with(progress, saturday, monkeypatch)
    entry, called = _fetch_idnes_with(progress, monday, monkeypatch)

    assert called["page_cursor"] == {}, "a new week reads from page one"
    assert entry["week"] == "2026-W39", "a new ISO week must be recorded"
    assert entry["sweep_started"]["byt/prodej"] == "2026-09-21", \
        "the sweep window must restart, or absence would be judged against last week"


def test_a_missing_week_marker_is_treated_as_a_new_week(monkeypatch):
    """State written by an older version must not be trusted as this week's."""
    from datetime import datetime, timezone

    progress = {"idnes": {"page_cursors": {"byt/prodej": 99}}}
    entry, called = _fetch_idnes_with(
        progress, datetime(2026, 9, 16, tzinfo=timezone.utc), monkeypatch)
    assert entry["week"] == "2026-W38"
    assert called["page_cursor"] == {}, "an unmarked cursor is not this week's"
