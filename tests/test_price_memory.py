"""Remembering what the donor adverts cost, so a lagged discount still pairs.

The case this exists for: a flat is discounted on sreality on Monday and on
iDNES on Tuesday. On Monday their current prices disagree and their histories
do not. Matching on a specific past price recovers that pair on evidence;
widening the tolerance recovers it by accepting anything nearby.
"""

from __future__ import annotations

from datetime import date

from common import price_memory


def test_todays_prices_are_remembered(tmp_path):
    donors = [{"source_id": "1", "price": 5_000_000},
              {"source_id": "2", "price": 18_000}]
    assert price_memory.remember(tmp_path, donors, date(2026, 9, 21)) == 2
    kept = price_memory.load(tmp_path)
    assert kept["2026-09-21"] == {"1": 5_000_000.0, "2": 18_000.0}


def test_yesterdays_price_is_still_available_today(tmp_path):
    price_memory.remember(tmp_path, [{"source_id": "1", "price": 5_000_000}],
                          date(2026, 9, 21))
    price_memory.remember(tmp_path, [{"source_id": "1", "price": 4_700_000}],
                          date(2026, 9, 22))
    kept = price_memory.load(tmp_path)
    assert price_memory.history_for(kept, "1") == [4_700_000.0, 5_000_000.0]


def test_a_price_outside_the_window_is_forgotten(tmp_path):
    """Three weeks ago is not evidence that two adverts are one flat today;
    it is evidence that two flats were once priced alike, which is common."""
    price_memory.remember(tmp_path, [{"source_id": "1", "price": 9_000_000}],
                          date(2026, 9, 1))
    price_memory.remember(tmp_path, [{"source_id": "1", "price": 5_000_000}],
                          date(2026, 9, 21))
    kept = price_memory.load(tmp_path)
    assert price_memory.history_for(kept, "1") == [5_000_000.0]


def test_the_window_covers_a_portal_lagging_by_days(tmp_path):
    for offset, price in ((5, 5_000_000), (0, 4_700_000)):
        price_memory.remember(
            tmp_path, [{"source_id": "1", "price": price}],
            date(2026, 9, 21) - __import__("datetime").timedelta(days=offset))
    kept = price_memory.load(tmp_path)
    assert 5_000_000.0 in price_memory.history_for(kept, "1")


def test_an_advert_without_a_price_is_not_recorded(tmp_path):
    n = price_memory.remember(
        tmp_path,
        [{"source_id": "1", "price": None}, {"source_id": "2", "price": ""},
         {"source_id": "", "price": 100}, {"source_id": "3", "price": "x"}],
        date(2026, 9, 21))
    assert n == 0
    assert price_memory.load(tmp_path)["2026-09-21"] == {}


def test_nothing_remembered_yet_is_not_an_error(tmp_path):
    assert price_memory.load(tmp_path) == {}
    assert price_memory.history_for({}, "1") == []


def test_unreadable_memory_is_treated_as_empty(tmp_path):
    path = price_memory.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert price_memory.load(tmp_path) == {}
    path.write_text("[]", encoding="utf-8")
    assert price_memory.load(tmp_path) == {}


def test_a_repeated_price_is_listed_once(tmp_path):
    for day in (19, 20, 21):
        price_memory.remember(tmp_path,
                              [{"source_id": "1", "price": 5_000_000}],
                              date(2026, 9, day))
    kept = price_memory.load(tmp_path)
    assert price_memory.history_for(kept, "1") == [5_000_000.0]
