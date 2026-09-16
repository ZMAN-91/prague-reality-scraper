import pytest

from common.schema import (
    MAX_MISSING_STREAK,
    STATUS_ACTIVE,
    STATUS_REMOVED,
    NormalizedListing,
    is_missing_status,
    make_internal_id,
    next_missing_status,
    price_per_m2,
    safe_float,
    safe_int,
)


def test_internal_id_is_deterministic():
    a = make_internal_id("sreality", "12345")
    b = make_internal_id("sreality", "12345")
    assert a == b


def test_internal_id_differs_by_source():
    a = make_internal_id("sreality", "12345")
    b = make_internal_id("bezrealitky", "12345")
    assert a != b


def test_missing_streak_progression_reaches_removed_after_configured_streak():
    status = STATUS_ACTIVE
    seen = [status]
    for _ in range(MAX_MISSING_STREAK + 2):
        status = next_missing_status(status)
        seen.append(status)
    # Exactly MAX_MISSING_STREAK consecutive misses before "removed".
    assert seen[MAX_MISSING_STREAK] == STATUS_REMOVED
    assert all(is_missing_status(s) for s in seen[1:MAX_MISSING_STREAK])
    # Removed is terminal - stays removed however many more misses happen.
    assert seen[-1] == STATUS_REMOVED


def test_removed_stays_removed():
    assert next_missing_status(STATUS_REMOVED) == STATUS_REMOVED


def test_reappearing_listing_resets_to_active_is_caller_responsibility():
    # next_missing_status only ever moves *away* from active; run.py itself
    # is responsible for snapping status back to "active" the moment a
    # listing is seen again (tested in tests/test_run.py).
    assert next_missing_status(STATUS_ACTIVE) != STATUS_ACTIVE


def test_price_per_m2_rounds_to_nearest_int():
    assert price_per_m2(1_000_000, 40) == 25_000


def test_price_per_m2_none_when_missing_inputs():
    assert price_per_m2(None, 40) is None
    assert price_per_m2(1_000_000, None) is None
    assert price_per_m2(1_000_000, 0) is None


def test_normalized_listing_rejects_unknown_property_type():
    with pytest.raises(ValueError):
        NormalizedListing(
            source="sreality",
            source_id="1",
            url="https://example.com",
            property_type="pozemek",
            transaction_type="prodej",
        )


def test_safe_float_parses_czech_formatted_numbers():
    assert safe_float("1 234,5") == 1234.5
    assert safe_float(None) is None
    assert safe_float("not a number") is None


def test_safe_int_rounds():
    assert safe_int("41.6") == 42


def test_price_per_m2_survives_an_index_row_without_an_area():
    """An index row that carries a price but no area must not blank out the
    price per m2 that an earlier detail fetch established.

    Otherwise the same listing shows a figure in one observation and nothing
    in the next, which reads as the number having changed when in fact only
    the source of the area did. Seen in the live data: 2 of 3054 observations.
    """
    from common.schema import NormalizedListing
    from run import merge_source

    def listing(area, price):
        return NormalizedListing(
            source="sreality", source_id="1", url="https://x/1",
            property_type="byt", transaction_type="prodej", disposition="2+kk",
            area_m2=area, price=price, lat=50.04, lon=14.48,
            address="Zabehlicka, Praha",
        )

    listings, last_obs, new_ids = {}, {}, []
    # First run: a detail fetch establishes the area.
    merge_source("sreality", [listing(56.0, 7_490_000)], [], listings, last_obs,
                 "2026-09-15T19:00:00+00:00", new_ids, set())
    # Second run: the index row has the new price but no area.
    _, observations = merge_source(
        "sreality", [listing(None, 7_350_000)], [], listings, last_obs,
        "2026-09-16T05:00:00+00:00", new_ids, set(),
    )

    assert len(observations) == 1, "the price change must be recorded"
    assert observations[0]["price"] == 7_350_000
    assert observations[0]["price_per_m2"], \
        "price per m2 went blank although the stored row still knows the area"
