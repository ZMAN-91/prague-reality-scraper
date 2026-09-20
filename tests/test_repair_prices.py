"""Putting back the prices a missed sweep erased.

A one-off repair, but not a one-off risk: it writes to the state every run
depends on, and the failure that matters is it overwriting a real sighting
with something older out of the log.
"""

import csv
import json
from pathlib import Path

import pytest

from tools import repair_prices


def observations(tmp_path, rows):
    directory = tmp_path / "observations"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "2026-09.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["internal_id", "observed_at", "price",
                                "price_per_m2", "status"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return directory


def obs(internal_id, observed_at, price, status="active"):
    return {"internal_id": internal_id, "observed_at": observed_at,
            "price": price, "price_per_m2": "", "status": status}


def test_a_blank_price_comes_back_from_the_log(tmp_path):
    directory = observations(tmp_path, [obs("a", "2026-09-19T10:00:00+00:00", "6940000")])
    prices = repair_prices.last_known_prices(directory)
    state, filled, blank = repair_prices.repair(
        {"a": {"price": None, "status": "missing_2"}}, prices)
    assert state["a"]["price"] == 6_940_000
    assert (filled, blank) == (1, 0)


def test_the_status_is_left_exactly_as_it_was(tmp_path):
    """The repair is about the price. Whether the listing is missing is not
    its business, and a listing that has gone must stay gone."""
    directory = observations(tmp_path, [obs("a", "2026-09-19T10:00:00+00:00", "6940000")])
    state, _, _ = repair_prices.repair(
        {"a": {"price": None, "status": "removed"}},
        repair_prices.last_known_prices(directory))
    assert state["a"]["status"] == "removed"


def test_a_price_already_in_the_state_is_never_overwritten(tmp_path):
    """A price in the state is a real sighting and is newer than anything the
    log can offer. Overwriting it would undo a price cut."""
    directory = observations(tmp_path, [obs("a", "2026-09-19T10:00:00+00:00", "6940000")])
    state, filled, _ = repair_prices.repair(
        {"a": {"price": 6_500_000, "status": "active"}},
        repair_prices.last_known_prices(directory))
    assert state["a"]["price"] == 6_500_000
    assert filled == 0


def test_the_newest_observed_price_wins(tmp_path):
    directory = observations(tmp_path, [
        obs("a", "2026-09-17T10:00:00+00:00", "7200000"),
        obs("a", "2026-09-19T10:00:00+00:00", "6940000"),
        obs("a", "2026-09-18T10:00:00+00:00", "7000000"),
    ])
    assert repair_prices.last_known_prices(directory)["a"] == 6_940_000


def test_absence_rows_do_not_count_as_a_price(tmp_path):
    """They are the rows that carry no price - reading them as one is the
    bug this repairs, arrived at from the other end."""
    directory = observations(tmp_path, [
        obs("a", "2026-09-19T10:00:00+00:00", "6940000"),
        obs("a", "2026-09-20T06:16:00+00:00", "", status="missing_1"),
    ])
    assert repair_prices.last_known_prices(directory)["a"] == 6_940_000


def test_a_listing_never_seen_with_a_price_is_counted_not_invented(tmp_path):
    directory = observations(tmp_path, [obs("a", "2026-09-19T10:00:00+00:00", "")])
    state, filled, blank = repair_prices.repair(
        {"a": {"price": None, "status": "active"}},
        repair_prices.last_known_prices(directory))
    assert state["a"]["price"] is None
    assert (filled, blank) == (0, 1)


def test_an_unparseable_price_is_skipped_rather_than_crashing(tmp_path):
    directory = observations(tmp_path, [
        obs("a", "2026-09-18T10:00:00+00:00", "6940000"),
        obs("a", "2026-09-19T10:00:00+00:00", "cena v RK"),
    ])
    assert repair_prices.last_known_prices(directory)["a"] == 6_940_000


def test_the_repaired_state_round_trips_through_the_project_writer(tmp_path):
    from common import storage
    path = tmp_path / "state" / "last_observation.json"
    storage.write_last_observation_state(
        {"a": {"price": None, "status": "missing_1"}}, path)
    directory = observations(tmp_path, [obs("a", "2026-09-19T10:00:00+00:00", "6940000")])
    state, _, _ = repair_prices.repair(
        storage.read_last_observation_state(path),
        repair_prices.last_known_prices(directory))
    storage.write_last_observation_state(state, path)
    assert storage.read_last_observation_state(path)["a"]["price"] == 6_940_000


def test_the_state_survives_the_commit_step_unchanged(tmp_path):
    """What run.py writes and what the commit step rewrites must be the same
    bytes.

    tools/reconcile.py runs on every commit and rewrites every JSON file it
    touches, so git only ever sees ITS formatting. When the two disagreed,
    the state file's diff was either 320 lines or 43,538 depending on which
    writer went last.
    """
    from common import storage
    from tools import reconcile

    state = {"b": {"price": 6_940_000, "status": "active"},
             "a": {"price": None, "status": "missing_1"}}
    path = tmp_path / "state" / "last_observation.json"
    storage.write_last_observation_state(state, path)
    written = path.read_text(encoding="utf-8")
    assert reconcile.merge_json(written, None) == written
    assert reconcile.merge_json(written, written) == written
