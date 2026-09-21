"""Per-run house numbers, for listings that arrive between index builds."""

from __future__ import annotations

import gzip

import pytest

import run as run_module
from common import ruian
from tests.test_ruian import STREET, at, index_of, point
from tools import build_ruian_index


@pytest.fixture
def data_dir(tmp_path):
    rows = []
    for entry in STREET.by_street["leopoldova"]:
        row = dict(entry)
        row["lat"] = f"{entry['lat']:.6f}"
        row["lon"] = f"{entry['lon']:.6f}"
        rows.append(row)
    build_ruian_index.write_index(rows, str(tmp_path / "ruian_praha.csv.gz"))
    return tmp_path


def row(north_m=0, east_m=30, ulice="Leopoldova", **extra):
    lat, lon = at(north_m, east_m)
    out = {"lat": str(lat), "lon": str(lon), "ulice": ulice}
    out.update(ruian.blank_match())
    out.update(extra)
    return out


def test_a_new_listing_gets_its_number_in_the_same_run(data_dir):
    listings = {"a": row()}
    assert run_module.fill_house_numbers(listings, data_dir) == 1
    assert listings["a"]["cislo_popisne"] == "2310"


def test_a_listing_that_already_has_one_is_left_alone(data_dir):
    """The monthly build re-does every row because the index changed
    underneath. Here it has not, so re-matching would spend time to reach the
    same answer."""
    listings = {"a": row(cislo_zdroj="ruian", cislo_popisne="9999")}
    assert run_module.fill_house_numbers(listings, data_dir) == 0
    assert listings["a"]["cislo_popisne"] == "9999"


def test_no_index_yet_is_not_an_error(tmp_path):
    """A scrape that runs before the first monthly build must still collect."""
    listings = {"a": row()}
    assert run_module.fill_house_numbers(listings, tmp_path) == 0
    assert listings["a"]["cislo_popisne"] == ""


def test_nothing_to_do_does_not_read_the_index(data_dir, monkeypatch):
    """The index costs 1.2 seconds and 180 MB. An hour whose listings all
    have numbers already should pay none of it."""
    def refuse(*args, **kwargs):
        raise AssertionError("the index was loaded with nothing to match")
    monkeypatch.setattr(ruian.Index, "load", staticmethod(refuse))
    listings = {"a": row(cislo_zdroj="ruian")}
    assert run_module.fill_house_numbers(listings, data_dir) == 0


def test_a_row_without_coordinates_is_skipped(data_dir):
    listings = {"a": row(), "b": {"lat": "", "lon": "", "ulice": "Leopoldova",
                                  **ruian.blank_match()}}
    assert run_module.fill_house_numbers(listings, data_dir) == 1


def test_a_row_whose_street_is_unknown_stays_empty(data_dir):
    listings = {"a": row(ulice="Neexistujici")}
    assert run_module.fill_house_numbers(listings, data_dir) == 0
    assert listings["a"]["cislo_zdroj"] == ""


def test_an_unparseable_coordinate_does_not_stop_the_rest(data_dir):
    listings = {"bad": row(), "good": row()}
    listings["bad"]["lat"] = "nekde"
    assert run_module.fill_house_numbers(listings, data_dir) == 1
    assert listings["good"]["cislo_popisne"] == "2310"
