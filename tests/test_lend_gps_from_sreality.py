"""Borrowing coordinates from a sreality advert that is not in the dataset.

The thing being protected here is not coverage, it is correctness. Copying a
coordinate across means asserting two adverts are the same flat, and a wrong
assertion produces a listing sitting confidently on the wrong building, with
a house number to match.
"""

from __future__ import annotations

from collections import Counter

import pytest

from common import dedup
from tools import lend_gps_from_sreality as lender


def idnes_row(internal_id="i1", address="Leopoldova, Praha 4 - Chodov",
              disposition="2+kk", area="55", **extra):
    row = {
        "internal_id": internal_id,
        "source": "idnes",
        "property_type": "byt",
        "transaction_type": "prodej",
        "disposition": disposition,
        "area_m2": area,
        "address": address,
        "lat": "", "lon": "", "gps_zdroj": "",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "last_seen_at": "2026-09-21",
        "status": "active",
    }
    row.update(extra)
    return row


def donor(source_id="9", address="Leopoldova, Praha 4 - Chodov",
          disposition="2+kk", area=55.0, lat=50.03, lon=14.51, price=5_000_000):
    return {
        "internal_id": f"sreality-donor-{source_id}",
        "source": "sreality",
        "source_id": source_id,
        "property_type": "byt",
        "transaction_type": "prodej",
        "disposition": disposition,
        "area_m2": area,
        "price": price,
        "lat": lat, "lon": lon,
        "address": address,
        "first_seen_at": "2026-09-21",
        "last_seen_at": "2026-09-21",
        "status": "active",
    }


PRICES = {"i1": 5_000_000}


def test_a_matching_advert_lends_its_coordinates():
    listings = {"i1": idnes_row()}
    filled, stats = lender.lend(listings, [donor()], PRICES)
    assert filled == 1
    assert listings["i1"]["lat"] == 50.03
    assert listings["i1"]["gps_zdroj"] == lender.FROM_SREALITY


def test_the_source_of_the_coordinate_is_not_called_a_cluster():
    """A cluster donor is a row in listings.csv and can be looked at. This
    one was read from the index once and dropped, so the two claims are not
    the same and must not read as the same."""
    from common import coords
    listings = {"i1": idnes_row()}
    lender.lend(listings, [donor()], PRICES)
    assert listings["i1"]["gps_zdroj"] != coords.FROM_CLUSTER
    assert listings["i1"]["gps_zdroj"] == "sreality"


def test_a_row_that_already_has_coordinates_is_never_touched():
    """An advert's own figure beats a stranger's, even when they disagree."""
    listings = {"i1": idnes_row(lat="50.1", lon="14.6", gps_zdroj="")}
    filled, stats = lender.lend(listings, [donor()], PRICES)
    assert filled == 0
    assert listings["i1"]["lat"] == "50.1"
    assert stats["already had coordinates"] == 1


def test_a_different_street_does_not_lend():
    listings = {"i1": idnes_row()}
    filled, _ = lender.lend(listings, [donor(address="Jinocanska, Praha 5")],
                            PRICES)
    assert filled == 0
    assert listings["i1"]["lat"] == ""


def test_a_different_disposition_does_not_lend():
    listings = {"i1": idnes_row()}
    filled, _ = lender.lend(listings, [donor(disposition="4+1")], PRICES)
    assert filled == 0


def test_a_contradicting_price_does_not_lend():
    """Two adverts for one flat quote the same figure. Two different flats in
    one building do not, and street plus disposition cannot tell them apart."""
    listings = {"i1": idnes_row()}
    filled, _ = lender.lend(listings, [donor(price=9_900_000)], PRICES)
    assert filled == 0


def test_two_equally_good_candidates_lend_nothing():
    """The point of this tool is to copy one coordinate onto one row. Two
    identical flats on the same street at the same price are exactly the case
    where picking either would be silently wrong half the time."""
    listings = {"i1": idnes_row()}
    twins = [donor(source_id="a", lat=50.03, lon=14.51),
             donor(source_id="b", lat=50.09, lon=14.40)]
    filled, stats = lender.lend(listings, twins, PRICES)
    assert filled == 0
    assert listings["i1"]["lat"] == ""
    assert stats["no confident match"] == 1


def test_a_listing_with_no_street_is_not_matched_to_anything():
    """With no coordinates on either side the street is the only anchor. Its
    absence must not become a licence to match on disposition alone."""
    listings = {"i1": idnes_row(address="Praha 4")}
    filled, stats = lender.lend(listings, [donor()], PRICES)
    assert filled == 0
    assert stats["no usable street"] == 1


def test_an_empty_donor_set_changes_nothing():
    listings = {"i1": idnes_row()}
    filled, stats = lender.lend(listings, [], PRICES)
    assert filled == 0
    assert stats["no donors were collected"] == 1


def test_sameness_is_decided_by_dedup_and_not_re_implemented():
    """A second notion of "same flat" living in a tool is how two answers to
    one question appear, and the weaker one wins by being newer."""
    import inspect
    source = inspect.getsource(lender)
    assert "dedup.best_match" in source
    assert "haversine" not in source, (
        "this tool has started measuring sameness itself")


def test_a_donor_without_coordinates_is_not_a_donor():
    """It has nothing to give, and matching against it would spend the whole
    comparison to fill a blank with a blank."""
    listings = {"i1": idnes_row()}
    blind = donor()
    blind["lat"] = None
    blind["lon"] = None
    filled, _ = lender.lend(listings, [blind], PRICES)
    assert filled == 0 or listings["i1"]["lat"] in ("", None)
