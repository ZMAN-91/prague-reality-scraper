"""The contracts between run.py and the scrapers it dispatches to.

These are the seams where a change to one side silently breaks the other:
the dispatch unpacks a different number of values for resumable sources, and
every fetcher has to agree on what it returns. A mismatch here is not a
subtle wrong number in a CSV - it is an exception in production at the top of
the hour, on a job nobody is watching.
"""

import inspect

import pytest

import run as run_module


def test_every_source_has_a_fetcher():
    from run import SOURCE_FETCHERS, parse_sources

    for name in parse_sources("sreality,idnes,bezrealitky"):
        assert name in SOURCE_FETCHERS, f"{name} is scheduled but has no fetcher"


def test_resumable_sources_are_a_subset_of_the_registered_ones():
    assert run_module.RESUMABLE_SOURCES <= set(run_module.SOURCE_FETCHERS)


@pytest.fixture
def stubbed_scrapers(monkeypatch):
    """Every underlying scraper replaced by one that returns the shape it
    promises, so the fetchers in run.py can be called for real."""
    from scrapers import bezrealitky, idnes, sreality

    monkeypatch.setattr(sreality, "fetch_all", lambda *a, **k: ([], [], [], set()))
    monkeypatch.setattr(bezrealitky, "fetch_all", lambda *a, **k: ([], [], [], set()))
    monkeypatch.setattr(idnes, "fetch_all", lambda *a, **k: ([], [], [], set(), 0, {}))
    monkeypatch.setattr(sreality.net, "polite_sleep", lambda *a, **k: None)


def test_a_resumable_fetcher_returns_the_five_values_the_dispatch_unpacks(stubbed_scrapers):
    """The dispatch unpacks five values for these and four for the rest, so
    adding a source to RESUMABLE_SOURCES without changing its return is an
    exception at the top of the hour, on a job nobody is watching."""
    from common.budget import Budget

    for name in run_module.RESUMABLE_SOURCES:
        fetcher = run_module.SOURCE_FETCHERS[name]
        parameters = list(inspect.signature(fetcher).parameters)
        assert "progress" in parameters, f"{name} is resumable but takes no progress"
        assert parameters.index("transactions") < parameters.index("progress"), (
            f"{name} takes progress before transactions; the dispatch passes them "
            "positionally, so the order is part of the contract"
        )
        result = fetcher(None, {}, Budget(max_seconds=1, max_new_details=1), None, ["prodej"], {})
        assert len(result) == 5, f"{name} returned {len(result)} values, dispatch unpacks 5"


def test_a_plain_fetcher_returns_the_four_values_the_dispatch_unpacks(stubbed_scrapers):
    from common.budget import Budget

    for name, fetcher in run_module.SOURCE_FETCHERS.items():
        if name in run_module.RESUMABLE_SOURCES:
            continue
        result = fetcher(None, {}, Budget(max_seconds=1, max_new_details=1), None, ["prodej"])
        assert len(result) == 4, f"{name} returned {len(result)} values, dispatch unpacks 4"


def test_the_resumable_dispatch_actually_passes_progress_through(stubbed_scrapers, tmp_path):
    """A resumable source that never receives the stored cursor restarts its
    sweep every hour and never finishes - silently, and only visibly as a
    sweep that mysteriously never completes."""
    from common import progress as progress_state

    seen = {}

    def spy(session, listings, budget, now=None, transactions=None, progress=None):
        seen["progress"] = progress
        seen["transactions"] = transactions
        return [], [], [], set(), None

    original = dict(run_module.SOURCE_FETCHERS)
    run_module.SOURCE_FETCHERS["idnes"] = spy
    try:
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        progress_state.write(data_dir, {"idnes": {"page_cursors": {"byt/prodej": 42}}})
        run_module.run(["idnes"], data_dir, tmp_path / "logs", max_seconds=1)
    finally:
        run_module.SOURCE_FETCHERS.clear()
        run_module.SOURCE_FETCHERS.update(original)

    assert seen["progress"]["idnes"]["page_cursors"]["byt/prodej"] == 42
    assert seen["transactions"] == ["prodej"], "the run's scope must reach the fetcher"


def test_an_unknown_source_is_skipped_rather_than_crashing(tmp_path, capsys):
    """A typo in the SOURCES variable must not take the whole run down with
    the sources that were spelled correctly."""
    exit_code = run_module.run(
        ["nonexistent"], tmp_path / "data", tmp_path / "logs", max_seconds=1,
    )
    assert exit_code == 0
    assert "unknown source" in capsys.readouterr().err


def test_a_crashing_source_does_not_take_the_others_down(tmp_path):
    """Last-resort safety net: an unanticipated bug in one scraper must not
    cost the run the data another one collected."""
    original = dict(run_module.SOURCE_FETCHERS)

    def explode(*args, **kwargs):
        raise RuntimeError("the portal returned something absurd")

    run_module.SOURCE_FETCHERS["boom"] = explode
    try:
        exit_code = run_module.run(
            ["boom"], tmp_path / "data", tmp_path / "logs", max_seconds=1,
        )
    finally:
        run_module.SOURCE_FETCHERS.clear()
        run_module.SOURCE_FETCHERS.update(original)

    assert exit_code == 1, "a crash must still be reported as a failure"
    assert (tmp_path / "data" / "listings.csv").exists(), "whatever was collected must still be written"
