"""Recognising the same flat when one of the portals publishes no coordinates.

Reality.iDNES.cz gives no GPS at all. The geometric matcher returns None the
moment either side is missing coordinates, so before this existed not one
iDNES listing could ever be recognised as the same flat as a sreality or
bezrealitky ad - in a project whose whole point is comparing one market
across portals.
"""

from datetime import datetime, timezone

import pytest

from common.dedup import cluster_listings, street_key

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


def listing(internal_id, source, address, *, lat=None, lon=None, area="55",
            disposition="2+kk", transaction="prodej", price=6_500_000):
    # Clustering requires the prices to AGREE, not merely to not contradict:
    # two adverts for one flat quote the same figure, two flats in one new
    # development do not. So a fixture that omits the price is a fixture that
    # must not cluster, and every one of these has to state it.
    return {
        "price": price,
        "internal_id": internal_id,
        "source": source,
        "source_id": internal_id,
        "property_type": "byt",
        "transaction_type": transaction,
        "disposition": disposition,
        "area_m2": area,
        "lat": lat or "",
        "lon": lon or "",
        "address": address,
        "first_seen_at": "2026-09-10T00:00:00+00:00",
        "last_seen_at": "2026-09-15",
        "status": "active",
        "cluster_id": "",
        "dedup_confidence": "",
    }


# --- the street key, across three different address spellings -------------


@pytest.mark.parametrize("address", [
    "Nurmiho, Hostivar, Praha",                # sreality: SEO slugs
    "Nurmiho 12, Praha 4 - Sporilov, Praha",   # bezrealitky: with house number
    "Nurmiho, Praha 15 - Hostivař",            # idnes: real diacritics
    "NURMIHO 1185/3, Praha",
])
def test_the_same_street_reads_the_same_from_every_source(address):
    assert street_key(address) == "nurmiho"


def test_diacritics_and_punctuation_do_not_split_a_street():
    assert street_key("Roztylské náměstí 5/12, Praha 4") == "roztylske namesti"


@pytest.mark.parametrize("address", ["Praha 4", "Praha", "", None, "  ,  "])
def test_an_address_with_no_street_yields_no_key(address):
    """A bare district would otherwise match half the city to itself."""
    assert street_key(address) == ""


# --- clustering across the gap --------------------------------------------


def test_a_listing_without_gps_clusters_with_one_that_has_it():
    rows = {
        "sreality": listing("sreality", "sreality", "Nurmiho, Hostivar, Praha",
                            lat="50.04525", lon="14.52430"),
        "idnes": listing("idnes", "idnes", "Nurmiho, Praha 15 - Hostivař"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["sreality"]["cluster_id"]
    assert rows["sreality"]["cluster_id"] == rows["idnes"]["cluster_id"]


def test_the_match_is_never_better_than_medium():
    """One block of flats can hold a dozen identical 2+kk units on one
    street, so a street match is real evidence but not the same evidence as
    two points forty metres apart."""
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15 - Hostivař"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["b"]["dedup_confidence"] == "medium"


def test_different_streets_do_not_cluster():
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "idnes", "Plzenska, Praha 5 - Smichov"),
    }
    cluster_listings(rows, now=NOW)
    assert not rows["b"]["cluster_id"] or rows["a"]["cluster_id"] != rows["b"]["cluster_id"]


def test_a_different_area_on_the_same_street_does_not_cluster():
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15 - Hostivař", area="120"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] != rows["b"]["cluster_id"] or not rows["a"]["cluster_id"]


def test_a_different_disposition_on_the_same_street_does_not_cluster():
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15 - Hostivař", disposition="4+1"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] != rows["b"]["cluster_id"] or not rows["a"]["cluster_id"]


def test_a_sale_never_clusters_with_a_rental():
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15 - Hostivař", transaction="pronajem"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] != rows["b"]["cluster_id"] or not rows["a"]["cluster_id"]


def test_two_listings_with_no_address_at_all_are_not_paired():
    """Otherwise every address-less listing in Prague joins one cluster."""
    rows = {
        "a": listing("a", "idnes", ""),
        "b": listing("b", "idnes", ""),
    }
    cluster_listings(rows, now=NOW)
    assert not rows["a"]["cluster_id"] and not rows["b"]["cluster_id"]


def test_three_portals_advertising_one_flat_land_in_one_cluster():
    """The case the user actually asked about: one flat, three agencies,
    three portals."""
    rows = {
        "s": listing("s", "sreality", "Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430"),
        "b": listing("b", "bezrealitky", "Nurmiho 12, Praha 15 - Hostivar, Praha",
                     lat="50.04527", lon="14.52433"),
        "i": listing("i", "idnes", "Nurmiho, Praha 15 - Hostivař"),
    }
    cluster_listings(rows, now=NOW)
    clusters = {row["cluster_id"] for row in rows.values()}
    assert len(clusters) == 1 and "" not in clusters


def test_a_pathologically_busy_street_is_skipped_rather_than_compared_pairwise():
    """Bounded explicitly, because "surely not" is how a quadratic blow-up
    gets into an hourly job nobody is watching."""
    from common.dedup import MAX_STREET_BUCKET

    rows = {
        str(n): listing(str(n), "idnes", "Developerska, Praha 9")
        for n in range(MAX_STREET_BUCKET + 5)
    }
    cluster_listings(rows, now=NOW)  # must simply return, not hang
    assert all(not row["cluster_id"] for row in rows.values())


# --- the same street name in two districts --------------------------------


def test_the_same_street_name_in_two_districts_does_not_cluster():
    """Nadrazni runs through both Smichov and Branik. With no coordinates to
    contradict it, street plus area plus disposition alone would merge two
    unrelated flats."""
    rows = {
        "a": listing("a", "sreality", "Nadrazni, Branik, Praha"),
        "b": listing("b", "idnes", "Nádražní, Praha 5 - Smíchov"),
    }
    cluster_listings(rows, now=NOW)
    assert not rows["a"]["cluster_id"] and not rows["b"]["cluster_id"]


def test_the_same_street_in_the_same_district_still_clusters():
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15 - Hostivař"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] and rows["a"]["cluster_id"] == rows["b"]["cluster_id"]


def test_an_address_that_names_no_district_falls_back_to_the_street():
    """"Nurmiho, Praha 15" says nothing beyond the street once the bare
    "praha" and the district number are dropped, so the street has to carry
    it rather than the pair being rejected."""
    rows = {
        "a": listing("a", "sreality", "Nurmiho, Hostivar, Praha"),
        "b": listing("b", "idnes", "Nurmiho, Praha 15"),
    }
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] and rows["a"]["cluster_id"] == rows["b"]["cluster_id"]


def test_locality_tokens_drop_the_words_shared_by_half_the_city():
    from common.dedup import locality_tokens

    assert locality_tokens("Nurmiho, Praha 15 - Hostivař") == {"hostivar"}
    assert locality_tokens("Nurmiho, Hostivar, Praha") == {"hostivar", "cz"} - {"cz"}
    assert locality_tokens("Nurmiho") == set()
    assert locality_tokens(None) == set()
