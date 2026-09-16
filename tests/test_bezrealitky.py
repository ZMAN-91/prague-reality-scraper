"""Tests for the bezrealitky scraper.

Field names in these fixtures are not invented: they come from the live
GraphQL `Advert` type, introspected once from CI (see the module docstring
in scrapers/bezrealitky.py). The page JSON embedded in __NEXT_DATA__ uses
the same object shape, which is why this scraper can take the route the
site's robots.txt permits and still get structured data.
"""

import json

from scrapers.bezrealitky import (
    _address_of,
    _is_probably_in_target_area,
    extract_advert_json,
    normalize_disposition,
    parse_advert,
)


def advert(**overrides):
    base = {
        "id": 1067272,
        "uri": "1067272-nabidka-prodej-bytu-nurniho-praha",
        "offerType": "PRODEJ",
        "estateType": "BYT",
        "disposition": "DISP_2_KK",
        "surface": 55,
        "etage": 3,
        "price": 6_500_000,
        "currency": "CZK",
        "description": "Hezky byt u parku.",
        "title": "Prodej bytu 2+kk",
        "gps": {"lat": 50.04440, "lng": 14.47890},  # Sporilov, resolved
        "street": "Nurniho",
        "houseNumber": "12",
        "city": "Praha",
        "cityDistrict": "Praha 4 - Sporilov",
        "zip": "14100",
        "active": True,
    }
    base.update(overrides)
    return base


# --- parsing a real advert object ----------------------------------------


def test_parse_advert_happy_path():
    listing = parse_advert(advert(), "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-x")
    assert listing is not None
    assert listing.source_id == "1067272"
    assert listing.property_type == "byt"
    assert listing.transaction_type == "prodej"
    assert listing.disposition == "2+kk"
    assert listing.area_m2 == 55.0
    assert listing.floor == 3
    assert listing.price == 6_500_000
    assert listing.lat == 50.04440
    assert listing.lon == 14.47890
    assert listing.in_target_area is True
    assert listing.priority_zone is True  # Sporilov


def test_parse_advert_builds_a_real_address_not_a_slug_guess():
    listing = parse_advert(advert(), "https://example.com/x")
    assert listing.address == "Nurniho 12, Praha 4 - Sporilov, Praha"


def test_parse_advert_address_degrades_gracefully():
    listing = parse_advert(
        advert(street=None, houseNumber=None, cityDistrict=None, city="Praha"), "https://example.com/x"
    )
    assert listing.address == "Praha"


def test_parse_advert_skips_out_of_scope_estate_types():
    """Land, garages and commercial space are outside this project's scope."""
    for estate_type in ("POZEMEK", "GARAZ", "KANCELAR", "NEBYTOVY_PROSTOR", "UNDEFINED"):
        assert parse_advert(advert(estateType=estate_type), "u") is None


def test_parse_advert_handles_both_offer_types():
    assert parse_advert(advert(offerType="PRONAJEM"), "u").transaction_type == "pronajem"
    assert parse_advert(advert(offerType="PRODEJ"), "u").transaction_type == "prodej"
    assert parse_advert(advert(offerType="UNDEFINED"), "u") is None


def test_parse_advert_without_id_returns_none():
    assert parse_advert(advert(id=None), "u") is None


def test_parse_advert_survives_missing_gps():
    listing = parse_advert(advert(gps=None), "u")
    assert listing is not None
    assert listing.lat is None
    assert listing.in_target_area is False


def test_parse_advert_tolerates_lon_instead_of_lng():
    listing = parse_advert(advert(gps={"lat": 50.08, "lon": 14.43}), "u")
    assert listing.lon == 14.43


def test_out_of_area_advert_is_flagged():
    brno = parse_advert(advert(gps={"lat": 49.1951, "lng": 16.6068}), "u")
    assert brno.in_target_area is False


# --- disposition mapping (what makes cross-source clustering possible) ----


def test_normalize_disposition_maps_onto_sreality_spelling():
    assert normalize_disposition("DISP_2_KK") == "2+kk"
    assert normalize_disposition("DISP_3_1") == "3+1"
    assert normalize_disposition("2+kk") == "2+kk"


def test_normalize_disposition_handles_garsoniera_and_unknowns():
    assert normalize_disposition("GARSONIERA") == "1+kk"
    assert normalize_disposition("SOMETHING_ELSE") == "something_else"
    assert normalize_disposition(None) is None


# --- __NEXT_DATA__ extraction --------------------------------------------


def page_with(payload: dict) -> str:
    return (
        "<!DOCTYPE html><html><head><title>x</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def test_extract_advert_json_finds_the_advert_anywhere_in_the_tree():
    html = page_with({"props": {"pageProps": {"advert": advert()}}})
    found = extract_advert_json(html)
    assert found is not None
    assert found["id"] == 1067272


def test_extract_advert_json_prefers_the_richest_candidate():
    """Detail pages also embed thin 'related advert' stubs; the page's own
    advert is the one with the most fields."""
    stub = {"id": 999, "uri": "999-x", "surface": 40}
    html = page_with({"props": {"pageProps": {"related": [stub], "advert": advert()}}})
    assert extract_advert_json(html)["id"] == 1067272


def test_extract_advert_json_returns_none_without_the_tag():
    assert extract_advert_json("<html><body>nothing here</body></html>") is None


def test_extract_advert_json_returns_none_on_malformed_json():
    html = '<script id="__NEXT_DATA__">{not valid json</script>'
    assert extract_advert_json(html) is None


# --- the cheap pre-filter that keeps the request count sane ---------------


def test_slug_prefilter_accepts_prague_and_the_surrounding_ring():
    assert _is_probably_in_target_area("1067272-nabidka-prodej-bytu-mezilehla-praha")
    assert _is_probably_in_target_area("1-nabidka-prodej-domu-jesenice")
    assert _is_probably_in_target_area("2-nabidka-pronajem-bytu-roztoky")


def test_slug_prefilter_rejects_far_away_cities():
    assert not _is_probably_in_target_area("1050320-nabidka-prodej-bytu-rehorova-brno")
    assert not _is_probably_in_target_area("1045164-nabidka-prodej-bytu-ceskoslovenske-armady-tabor")
    assert not _is_probably_in_target_area("961801-nabidka-prodej-bytu-zamecky-vrch-karlovy-vary")


def test_address_of_prefers_structured_fields_over_the_free_text_one():
    assert _address_of({"street": "Nurniho", "city": "Praha", "address": "ignored"}) == "Nurniho, Praha"
    assert _address_of({"address": "Nejaka adresa"}) == "Nejaka adresa"
    assert _address_of({}) is None


# --- presence from the sitemap (what makes tiering safe) ------------------

from scrapers.bezrealitky import meta_from_url, presence_listing


def test_meta_from_url_reads_id_type_and_offer_from_the_slug():
    url = "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha"
    assert meta_from_url(url) == ("1067272", "byt", "prodej")

    rent_house = "https://www.bezrealitky.cz/nemovitosti-byty-domy/42-nabidka-pronajem-domu-sporilov-praha"
    assert meta_from_url(rent_house) == ("42", "dum", "pronajem")


def test_meta_from_url_returns_none_for_an_unreadable_slug():
    assert meta_from_url("https://www.bezrealitky.cz/nemovitosti-byty-domy/7-nabidka-pozemku-praha") is None
    assert meta_from_url("https://www.bezrealitky.cz/vyhledat") is None


def test_presence_listing_proves_life_without_claiming_content():
    """The point of a presence-only sighting: it keeps a listing alive under
    a tiered schedule without pretending to know its current price."""
    url = "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha"
    listing = presence_listing(url)
    assert listing is not None
    assert listing.presence_only is True
    assert listing.source_id == "1067272"
    assert listing.price is None and listing.area_m2 is None
    assert listing.in_target_area is True


def test_presence_only_sighting_keeps_a_listing_active_without_logging_a_price():
    """Regression guard for the trap tiering creates: a listing not re-read
    this hour must neither be marked missing nor logged as having lost its
    price."""
    from run import merge_source

    listings, last_obs = {}, {}
    full = parse_advert(advert(), "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha")
    scopes = {("byt", "prodej")}
    merge_source("bezrealitky", [full], [], listings, last_obs, "2026-03-01T10:00:00+00:00", [], scopes)
    assert len(listings) == 1

    presence = presence_listing("https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha")
    stats, obs, _ = merge_source(
        "bezrealitky", [presence], [], listings, last_obs, "2026-03-02T10:00:00+00:00", [], scopes
    )

    row = next(iter(listings.values()))
    assert row["status"] == "active", "a sitemap sighting must keep the listing alive"
    assert row["last_seen_at"] == "2026-03-02"
    assert row["area_m2"] == 55.0, "presence-only must not wipe known content"
    assert obs == [], "no content was read, so there is nothing to observe"


# --- city-wide hourly rotation -------------------------------------------


def test_revisit_queue_is_ordered_least_recently_seen_first():
    """When a run cannot cover all of Prague in one hour, it must not cover
    the same head of the list every hour and never reach the tail."""
    from scrapers import bezrealitky as bz

    urls = [f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{n}-nabidka-prodej-bytu-praha" for n in range(1, 5)]
    order = {urls[0]: "2026-03-05", urls[1]: "2026-03-01", urls[2]: "2026-03-09", urls[3]: "2026-03-03"}

    captured = {}

    def fake_sitemap(session, budget=None):
        return list(urls), []

    def fake_fetch(session, url):
        captured.setdefault("visited", []).append(url)
        return None, None

    original_sitemap, original_fetch, original_sleep = (
        bz.iter_sitemap_listing_urls, bz.fetch_listing, bz.net.polite_sleep
    )
    bz.iter_sitemap_listing_urls, bz.fetch_listing, bz.net.polite_sleep = (
        fake_sitemap, fake_fetch, lambda *a, **k: None
    )
    try:
        bz.fetch_all(
            session=None,
            known_urls=set(urls),
            due_urls=set(urls),
            revisit_order=order,
        )
    finally:
        bz.iter_sitemap_listing_urls, bz.fetch_listing, bz.net.polite_sleep = (
            original_sitemap, original_fetch, original_sleep
        )

    assert captured["visited"] == [urls[1], urls[3], urls[0], urls[2]]


def test_new_listings_in_the_watched_area_are_fetched_before_the_rest():
    """A listing is only a sitemap URL until it is fetched, so the slug is
    all there is to prioritise on."""
    from common import collection_area

    assert collection_area.name_suggests_area(
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/1-nabidka-prodej-bytu-nurmiho-praha")
    assert not collection_area.name_suggests_area(
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/2-nabidka-prodej-bytu-modrany-praha")
