"""Planned stops must not look like failures.

This module exists because of a real incident: a run that did exactly what
it was designed to do - collect for its hour and stop - exited non-zero, the
workflow failed on purpose to raise an alert, and the alert said the scrape
had failed. The second, worse half of that: a genuine outage would have
produced an identical alert, so the notification carried no information
either way.
"""

from common import interruptions


def test_a_marked_message_is_recognised_as_a_planned_stop():
    message = interruptions.interruption("bezrealitky: sweep stopped - time budget exhausted")
    assert interruptions.is_interruption(message)
    assert "time budget exhausted" in message


def test_an_ordinary_error_is_not_a_planned_stop():
    assert not interruptions.is_interruption("sreality byt/prodej offset=500: HTTP 503")


def test_split_separates_the_two_and_keeps_their_order():
    messages = [
        "sreality: HTTP 503",
        interruptions.interruption("idnes: rotation stopped at page 40"),
        "bezrealitky: malformed JSON",
        interruptions.interruption("bezrealitky: sweep stopped"),
    ]
    errors, stops = interruptions.split(messages)
    assert errors == ["sreality: HTTP 503", "bezrealitky: malformed JSON"]
    assert [interruptions.strip_marker(s) for s in stops] == [
        "idnes: rotation stopped at page 40",
        "bezrealitky: sweep stopped",
    ]


def test_split_handles_nothing_at_all():
    assert interruptions.split([]) == ([], [])
    assert interruptions.split(None) == ([], [])


def test_strip_marker_leaves_a_real_error_untouched():
    assert interruptions.strip_marker("sreality: HTTP 503") == "sreality: HTTP 503"


def test_every_scraper_marks_its_budget_stop():
    """A stop message that forgets the marker silently becomes a failing run,
    so each scraper's own wording is checked rather than trusted."""
    import inspect

    from scrapers import bezrealitky, idnes, sreality

    for module in (sreality, bezrealitky, idnes):
        lines = inspect.getsource(module).splitlines()
        for line_number, line in enumerate(lines, 1):
            if "budget exhausted" not in line:
                continue
            # The append and the message are often on different lines, so
            # look at a small window rather than the one line.
            window = "\n".join(lines[max(0, line_number - 4):line_number + 1])
            if "errors.append" not in window:
                continue
            assert "interruption" in window, (
                f"{module.__name__}:{line_number} reports a budget stop as an error"
            )


def test_a_budget_stop_does_not_fail_the_run(tmp_path, monkeypatch):
    """End to end: the exact shape of the run that sent the email."""
    from run import merge_source

    listings, last_obs = {}, {}
    stats = merge_source(
        "bezrealitky",
        [],
        [interruptions.interruption("bezrealitky: listing sweep stopped - run time budget exhausted")],
        listings,
        last_obs,
        "2026-09-15T15:29:37+00:00",
        [],
        set(),
    )[0]
    assert stats["errors"] == [], "a planned stop must not be reported as an error"
    assert stats["interruptions"] == [
        "bezrealitky: listing sweep stopped - run time budget exhausted"
    ]


def test_a_real_error_still_fails_the_run():
    from run import merge_source

    stats = merge_source(
        "sreality", [], ["sreality byt/prodej offset=0: HTTP 503"], {}, {},
        "2026-09-15T15:29:37+00:00", [], set(),
    )[0]
    assert stats["errors"] == ["sreality byt/prodej offset=0: HTTP 503"]
    assert stats["interruptions"] == []


# --- the silent death this project is most likely to suffer ---------------


def test_a_source_that_suddenly_returns_nothing_is_an_error():
    """A portal changes a CSS class, the parser matches nothing, every
    request still returns HTTP 200, and the dataset quietly stops growing.
    Now that a planned stop no longer fails the run, nothing else would ever
    notice."""
    from common.schema import STATUS_ACTIVE, make_internal_id
    from run import merge_source

    stored = {
        make_internal_id("idnes", "1"): {
            "internal_id": make_internal_id("idnes", "1"),
            "source": "idnes", "source_id": "1", "url": "u",
            "property_type": "byt", "transaction_type": "prodej",
            "disposition": "2+kk", "area_m2": "55", "lat": "", "lon": "",
            "address": "Ulice, Praha 4", "priority_zone": "False",
            "description": "", "first_seen_at": "2026-09-01T00:00:00+00:00",
            "last_seen_at": "2026-09-15", "status": STATUS_ACTIVE,
            "cluster_id": "", "dedup_confidence": "", "relisted_from": "", "floor": "",
        }
    }
    stats, _, _ = merge_source("idnes", [], [], stored, {}, "2026-09-16T09:00:00+00:00", [], set())
    assert stats["errors"], "an empty result with stored listings must be reported"
    assert "stopped matching" in stats["errors"][0]


def test_an_empty_result_after_a_planned_stop_is_not_an_error():
    """Out of budget before the source got going is not a broken parser."""
    from run import merge_source

    stored = {"x": {"internal_id": "x", "source": "idnes", "source_id": "1", "url": "u",
                    "property_type": "byt", "transaction_type": "prodej", "disposition": "2+kk",
                    "area_m2": "55", "lat": "", "lon": "", "address": "U, Praha 4",
                    "priority_zone": "False", "description": "", "floor": "",
                    "first_seen_at": "2026-09-01T00:00:00+00:00", "last_seen_at": "2026-09-15",
                    "status": "active", "cluster_id": "", "dedup_confidence": "", "relisted_from": ""}}
    stats, _, _ = merge_source(
        "idnes", [], [interruptions.interruption("idnes: time budget exhausted")],
        stored, {}, "2026-09-16T09:00:00+00:00", [], set(),
    )
    assert stats["errors"] == []


def test_the_very_first_run_of_a_source_is_not_an_error():
    """Zero is simply where a new source starts."""
    from run import merge_source

    stats, _, _ = merge_source("idnes", [], [], {}, {}, "2026-09-16T09:00:00+00:00", [], set())
    assert stats["errors"] == []
