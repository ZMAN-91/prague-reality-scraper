"""The pure parsing functions in scrapers/sreality.py.

The fixture shapes are not guesses any more: the sreality v1 API was probed
live (tools/probe_sources.py) and these mirror what it actually returns,
including the {"name", "value"} enum objects and the `locality` block that
carries GPS on an index row - the discovery that turned a sweep of Prague
from ~12 000 requests into ~35.

What these still cannot prove is that the live API has not changed since.
That is what the empty-result alarm in run.merge_source is for.
"""

from scrapers import sreality
from scrapers.sreality import (
    _build_source_url,
    _cb_value,
    _disposition,
    _extract_index_id,
    _extract_index_price,
    _fallback_url,
    _unwrap_estate,
)


def _cb(value, name=None):
    return {"value": value, "name": name}


def test_cb_value_extracts_int_and_treats_zero_as_unspecified():
    assert _cb_value({"value": 4, "name": "Byt"}) == 4
    assert _cb_value({"value": 0, "name": "nezadano"}) is None
    assert _cb_value(None) is None
    assert _cb_value("not a dict") is None


def test_unwrap_estate_handles_flat_and_wrapped_envelopes():
    flat = {"category_main_cb": _cb(1), "hash_id": 1}
    assert _unwrap_estate(flat) is flat

    wrapped = {"result": {"category_main_cb": _cb(1), "hash_id": 1}, "status_code": 200}
    assert _unwrap_estate(wrapped) == wrapped["result"]

    # No estate marker anywhere - returns the payload unchanged so the
    # caller's own "category_main_cb" not in raw check catches it.
    envelope_only = {"status_code": 200, "status_message": "Not found"}
    assert _unwrap_estate(envelope_only) == envelope_only


def test_disposition_prefers_category_sub_cb_name_then_advert_name():
    raw = {"category_sub_cb": _cb(4, "2+kk"), "advert_name": "Prodej bytu 3+1"}
    assert _disposition(raw) == "2+kk"

    raw_fallback = {"category_sub_cb": _cb(0, None), "advert_name": "Prodej bytu 3+1 80 m2"}
    assert _disposition(raw_fallback) == "3+1"

    assert _disposition({}) is None


def test_extract_index_id_and_price_try_multiple_keys():
    assert _extract_index_id({"hash_id": 123456}) == "123456"
    assert _extract_index_id({"id": 999}) == "999"
    assert _extract_index_id({}) is None

    assert _extract_index_price({"price_summary_czk": 6_500_000}) == 6_500_000
    assert _extract_index_price({"price_czk": 25000}) == 25000
    assert _extract_index_price({}) is None


def test_build_source_url_happy_path():
    raw = {
        "hash_id": 123456,
        "category_main_cb": _cb(1),  # byt
        "category_type_cb": _cb(1),  # prodej
        "category_sub_cb": _cb(4),  # 2+kk
        "locality": {
            "city_seo_name": "praha",
            "citypart_seo_name": "praha-10-strasnice",
            "street_seo_name": "korunni",
        },
    }
    url = _build_source_url(raw)
    assert url == "https://www.sreality.cz/detail/prodej/byt/2+kk/praha-praha-10-strasnice-korunni/123456"


def test_build_source_url_repeats_city_when_citypart_missing():
    raw = {
        "hash_id": 1,
        "category_main_cb": _cb(2),  # dum
        "category_type_cb": _cb(2),  # pronajem
        "category_sub_cb": _cb(37),  # rodinny
        "locality": {"city_seo_name": "prestavlky"},
    }
    url = _build_source_url(raw)
    assert url == "https://www.sreality.cz/detail/pronajem/dum/rodinny/prestavlky-prestavlky-/1"


def test_build_source_url_returns_none_when_sub_cb_unmapped_or_mismatched():
    base = {
        "hash_id": 1,
        "category_main_cb": _cb(1),
        "category_type_cb": _cb(1),
        "locality": {"city_seo_name": "praha"},
    }
    assert _build_source_url({**base, "category_sub_cb": _cb(999)}) is None  # unmapped code
    assert _build_source_url({**base, "category_sub_cb": _cb(37)}) is None  # dum code on a byt listing
    assert _build_source_url({**base, "category_sub_cb": _cb(0)}) is None  # unspecified


def test_build_source_url_returns_none_without_city():
    raw = {
        "hash_id": 1,
        "category_main_cb": _cb(1),
        "category_type_cb": _cb(1),
        "category_sub_cb": _cb(4),
        "locality": {},
    }
    assert _build_source_url(raw) is None


def test_fallback_url_is_well_formed_and_never_raises():
    url = _fallback_url("byt", "prodej", "Praha Vinohrady", "999")
    assert url.startswith("https://www.sreality.cz/detail/prodej/byt/")
    assert url.endswith("/999")

    # No address text at all - still produces something usable.
    url_no_address = _fallback_url("dum", "pronajem", None, "1")
    assert "pronajem/dum" in url_no_address


# --- narrowing the sreality walk to the watched area ------------------------


def test_no_area_district_is_the_whole_of_prague():
    """47 is all of Prague. Left in a list meant to narrow, it would make the
    narrowing a no-op that still looked configured."""
    assert sreality.DISTRICT_PRAHA not in sreality.AREA_DISTRICT_IDS


def test_zero_is_never_an_area_district():
    """locality_district_id=0 is not refused by this API - it answers with all
    20,135 listings in the country. A zero here would silently turn an hourly
    area sweep into a national one, and nothing in the response would say so.
    """
    assert 0 not in sreality.AREA_DISTRICT_IDS


def test_the_area_ids_are_a_finer_space_than_the_walked_districts():
    """47/56/57 are okresy; the area ids are the 50xx postal districts inside
    Prague. Mixing the two numbering systems in one tuple would send a walk
    to whichever the API happened to match."""
    assert all(d >= 5000 for d in sreality.AREA_DISTRICT_IDS), \
        sreality.AREA_DISTRICT_IDS


def test_the_two_portals_narrow_to_the_same_two_districts():
    """Both use POSTAL districts, so the watched area is Praha 4 and Praha 10
    on each. Confirmed by what they return, not by their labels: sreality's
    5004 answers with Chodov (city district Praha 11) and 5010 with Horni
    Mecholupy and Petrovice (Praha 15), exactly as iDNES's praha-4 and
    praha-10 branches do.

    If one side is ever narrowed differently from the other, the hourly pass
    collects a different area from each portal and the cross-source dedup
    starts comparing two populations that do not overlap.
    """
    from scrapers import idnes

    sreality_numbers = sorted(str(d)[-2:].lstrip("0")
                              for d in sreality.AREA_DISTRICT_IDS)
    idnes_numbers = sorted(b.removeprefix("praha-") for b in idnes.AREA_BRANCHES)
    assert sreality_numbers == idnes_numbers == ["10", "4"], (
        sreality_numbers, idnes_numbers)
