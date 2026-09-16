"""Sale and rent are collected separately, on different schedules and scopes.

Sale runs every hour, and on sreality only inside the watched area - that
restriction is what makes an hourly sreality sweep a small thing rather than
a copy of the city. Rent runs once a week and covers the whole of Prague,
because the reason for keeping sreality small does not hold for something
that happens once every seven days.

The thing most worth pinning here is that a run collecting one side does not
disturb the other: an hourly sale run must never conclude that every rental
in Prague has vanished just because it did not look at any.
"""

import pytest

from common.schema import STATUS_ACTIVE, make_internal_id
from run import (
    DEFAULT_TRANSACTIONS,
    merge_source,
    parse_transactions,
    sreality_is_area_limited,
)


# --- what a run is asked to collect ---------------------------------------


def test_the_default_is_sale_only():
    assert list(DEFAULT_TRANSACTIONS) == ["prodej"]
    assert parse_transactions("") == ["prodej"]


def test_transaction_names_are_validated_rather_than_silently_dropped():
    """A typo would filter every listing away and the run would look merely
    empty, which is the worst way for a mistake to present itself."""
    with pytest.raises(SystemExit) as excinfo:
        parse_transactions("prodej,pronajm")
    assert "pronajm" in str(excinfo.value)


def test_both_can_be_asked_for_together():
    assert parse_transactions("prodej,pronajem") == ["prodej", "pronajem"]


# --- scope follows the transaction ----------------------------------------


def test_sale_is_confined_to_the_watched_area():
    assert sreality_is_area_limited(["prodej"]) is True


def test_rent_covers_the_whole_city():
    assert sreality_is_area_limited(["pronajem"]) is False


def test_a_mixed_run_is_not_area_limited():
    """Asking for both means the city-wide half decides, or the rent listings
    outside the belt would be silently dropped."""
    assert sreality_is_area_limited(["prodej", "pronajem"]) is False


# --- the scrapers actually honour it --------------------------------------


def test_sreality_walks_only_the_transaction_it_was_asked_for(monkeypatch):
    from scrapers import sreality

    walked = []

    def fake_fetch_json(session, url, params=None, **kwargs):
        walked.append(params["category_type_cb"])
        return {"pagination": {"total": 0, "limit": 500}, "results": []}

    monkeypatch.setattr(sreality.net, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(sreality.net, "polite_sleep", lambda *a, **k: None)

    sreality.fetch_all(None, None, transactions=["prodej"])
    assert set(walked) == {sreality.CATEGORY_TYPE["prodej"]}, "rent must not be walked"


def test_bezrealitky_drops_the_other_half_before_fetching_anything(monkeypatch):
    """The slug says which it is, so an unwanted listing costs no request."""
    from scrapers import bezrealitky as bz

    urls = [
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/1-nabidka-prodej-bytu-praha",
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/2-nabidka-pronajem-bytu-praha",
    ]
    fetched = []
    monkeypatch.setattr(bz, "iter_sitemap_listing_urls", lambda s, b=None: (list(urls), []))
    monkeypatch.setattr(bz, "fetch_listing", lambda s, u: (fetched.append(u), (None, None))[1])
    monkeypatch.setattr(bz.net, "polite_sleep", lambda *a, **k: None)

    bz.fetch_all(None, transactions=["pronajem"])
    assert fetched == [urls[1]]


def test_idnes_reads_only_the_requested_search_pages(monkeypatch):
    from common.budget import Budget
    from scrapers import idnes

    requested = []
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        idnes.net, "fetch_text",
        lambda session, url, **kw: (requested.append(url), "<html>nic</html>")[1],
    )
    idnes.fetch_all(None, Budget(max_seconds=60, max_new_details=None),
                    known={}, page_cursor={}, max_pages=2, transactions=["pronajem"])
    assert requested, "something must be fetched"
    assert all("/pronajem/" in url for url in requested), requested


# --- the two halves must not disturb each other ---------------------------


def row(source_id, transaction):
    return {
        "internal_id": make_internal_id("sreality", source_id),
        "source": "sreality", "source_id": source_id, "url": "u",
        "property_type": "byt", "transaction_type": transaction,
        "disposition": "2+kk", "area_m2": "55", "floor": "",
        "lat": "50.045", "lon": "14.524", "address": "Nurmiho, Praha",
        "priority_zone": "True", "description": "",
        "first_seen_at": "2026-09-01T00:00:00+00:00", "last_seen_at": "2026-09-15",
        "status": STATUS_ACTIVE, "cluster_id": "", "dedup_confidence": "",
        "relisted_from": "",
    }


def test_a_sale_run_never_marks_rentals_missing():
    """The failure this guards against: an hourly sale run concluding that
    every rental in Prague has vanished, because it did not look at any."""
    listings = {
        make_internal_id("sreality", "sale"): row("sale", "prodej"),
        make_internal_id("sreality", "rent"): row("rent", "pronajem"),
    }
    merge_source(
        "sreality", [], [], listings, {}, "2026-09-16T09:00:00+00:00", [],
        completed_scopes={("byt", "prodej")},
    )
    assert listings[make_internal_id("sreality", "rent")]["status"] == STATUS_ACTIVE
    assert listings[make_internal_id("sreality", "sale")]["status"] == "missing_1"


def test_a_rent_run_never_marks_sales_missing():
    listings = {
        make_internal_id("sreality", "sale"): row("sale", "prodej"),
        make_internal_id("sreality", "rent"): row("rent", "pronajem"),
    }
    merge_source(
        "sreality", [], [], listings, {}, "2026-09-16T09:00:00+00:00", [],
        completed_scopes={("byt", "pronajem")},
    )
    assert listings[make_internal_id("sreality", "sale")]["status"] == STATUS_ACTIVE
    assert listings[make_internal_id("sreality", "rent")]["status"] == "missing_1"


def test_both_kinds_live_in_one_dataset():
    """They are separated by schedule, not by file: one listings.csv, told
    apart by transaction_type, so a price comparison can span both."""
    from common.schema import LISTING_FIELDS

    assert "transaction_type" in LISTING_FIELDS
