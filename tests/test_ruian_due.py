"""When the address index is rebuilt, and - mostly - when it is not.

The failure this guards against is not a wasted build. It is an index that
quietly stops being rebuilt and goes on answering with addresses from years
ago, which looks exactly like an index that is working.
"""

from __future__ import annotations

import json
from datetime import date

from tools import ruian_due


def meta(export: str, rows: int = 134_000, built_at: str = "") -> str:
    body = {"export": export, "rows": rows}
    if built_at:
        body["built_at"] = built_at
    return json.dumps(body)


def test_no_index_at_all_is_due():
    due, why = ruian_due.is_due(None, date(2026, 9, 21))
    assert due and "no index" in why


def test_the_newest_export_there_is_is_not_due():
    """On 21 September the newest possible export is 31 August."""
    due, _ = ruian_due.is_due(meta("2026-08-31"), date(2026, 9, 21))
    assert not due


def test_the_month_turning_over_makes_it_due():
    """Same index, one day into October: 30 September now exists."""
    index = meta("2026-08-31", built_at="2026-09-02")
    assert not ruian_due.is_due(index, date(2026, 9, 30))[0]
    assert ruian_due.is_due(index, date(2026, 10, 1))[0]


def test_a_late_publication_is_tried_once_and_then_left_alone():
    """1 October wants the 30 September export. If it is not out the builder
    writes 31 August again - and every remaining attempt in the window would
    re-download three megabytes to learn the same thing."""
    stale = meta("2026-08-31", built_at="2026-10-01")
    assert not ruian_due.is_due(stale, date(2026, 10, 1))[0]


def test_it_is_tried_again_the_next_day():
    """One attempt a day, not one attempt ever. An export that appears on the
    3rd is picked up on the 3rd."""
    stale = meta("2026-08-31", built_at="2026-10-01")
    due, why = ruian_due.is_due(stale, date(2026, 10, 2))
    assert due, why
    assert "nothing has been tried today" in why


def test_an_index_that_is_never_rebuilt_stays_due_every_day():
    """The failure mode this exists for. With an age tolerance instead, an
    index that stopped rebuilding would go quiet for weeks at a time."""
    for day in range(2, 20):
        stale = meta("2026-08-31", built_at=f"2026-10-{day - 1:02d}")
        assert ruian_due.is_due(stale, date(2026, 10, day))[0], day


def test_a_successful_rebuild_stops_the_daily_retries():
    fresh = meta("2026-09-30", built_at="2026-10-03")
    for day in range(3, 20):
        assert not ruian_due.is_due(fresh, date(2026, 10, day))[0], day


def test_an_index_with_no_attempt_recorded_is_due():
    """Written by a builder from before the sidecar carried built_at. Due is
    the safe reading: it costs one download and produces a sidecar that does
    carry it."""
    due, _ = ruian_due.is_due(meta("2026-08-31"), date(2026, 10, 5))
    assert due


def test_metadata_that_cannot_be_read_is_due():
    for broken in ("", "not json", "[]", "null", '"a string"', "7"):
        due, why = ruian_due.is_due(broken, date(2026, 9, 21))
        assert due, f"{broken!r} was treated as a usable index: {why}"


def test_metadata_without_an_export_is_due():
    due, _ = ruian_due.is_due(json.dumps({"rows": 134_000}), date(2026, 9, 21))
    assert due
    due, _ = ruian_due.is_due(json.dumps({"export": "soon", "rows": 1}),
                              date(2026, 9, 21))
    assert due


def test_an_index_reporting_no_rows_is_due():
    """A build that wrote a header and nothing else must not count as one
    that succeeded - it would match nothing, for ever, silently."""
    due, why = ruian_due.is_due(meta("2026-08-31", rows=0), date(2026, 9, 21))
    assert due and "no rows" in why


def test_the_previous_month_end_is_the_month_end(): 
    assert ruian_due.previous_month_end(date(2026, 9, 21)) == date(2026, 8, 31)
    assert ruian_due.previous_month_end(date(2026, 3, 1)) == date(2026, 2, 28)
    assert ruian_due.previous_month_end(date(2028, 3, 15)) == date(2028, 2, 29)
    assert ruian_due.previous_month_end(date(2026, 1, 10)) == date(2025, 12, 31)


def test_the_command_prints_what_a_workflow_step_reads(tmp_path, capsys):
    index = tmp_path / "ruian_praha.csv.gz"
    assert ruian_due.main(["--index", str(index),
                           "--today", "2026-09-21"]) == 0
    assert "go=true" in capsys.readouterr().out

    (tmp_path / "ruian_praha.csv.gz.json").write_text(meta("2026-08-31"),
                                                      encoding="utf-8")
    assert ruian_due.main(["--index", str(index),
                           "--today", "2026-09-21"]) == 0
    assert "go=false" in capsys.readouterr().out
