from datetime import datetime, timezone

from common.dedup import cluster_listings, find_relist_candidate

# Clustering only derives new clusters among *recently seen* rows (see
# CLUSTER_RECENT_WINDOW_DAYS), so fixtures pin both their timestamps and the
# clock the clustering pass compares them against. Without pinning, these
# tests would quietly stop testing anything the moment the fixture dates
# aged past the window.
FIXTURE_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def cluster(listings):
    cluster_listings(listings, now=FIXTURE_NOW)


def make_row(
    internal_id,
    source="sreality",
    property_type="byt",
    transaction_type="prodej",
    disposition="2+kk",
    area_m2=55.0,
    lat=50.0755,
    lon=14.4378,
    status="active",
    last_seen_at="2026-01-01T12:00:00+00:00",
    cluster_id="",
    price=None,
):
    return {
        "internal_id": internal_id,
        # Not a listings.csv column - price lives in observations - but
        # cluster_listings is given it by the caller, because it is the only
        # field that tells eight flats in one development apart from eight
        # ads for one flat.
        "price": price,
        "source": source,
        "source_id": internal_id,
        "url": f"https://example.com/{internal_id}",
        "property_type": property_type,
        "transaction_type": transaction_type,
        "disposition": disposition,
        "area_m2": area_m2,
        "floor": "",
        "lat": lat,
        "lon": lon,
        "address": "Praha",
        "priority_zone": False,
        "description": "",
        "first_seen_at": "2026-01-01T12:00:00+00:00",
        "last_seen_at": last_seen_at,
        "status": status,
        "cluster_id": cluster_id,
        "relisted_from": "",
    }


def test_near_identical_listings_from_two_sources_cluster_exact():
    listings = {
        "a": make_row("a", source="sreality", lat=50.0755, lon=14.4378, area_m2=55.0),
        "b": make_row("b", source="bezrealitky", lat=50.07551, lon=14.43781, area_m2=55.2),
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] != ""
    assert listings["a"]["cluster_id"] == listings["b"]["cluster_id"]
    assert listings["a"]["dedup_confidence"] == "exact"


def test_different_disposition_never_clusters_even_if_colocated():
    listings = {
        "a": make_row("a", disposition="2+kk"),
        "b": make_row("b", disposition="3+kk"),
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] == ""
    assert listings["b"]["cluster_id"] == ""


def test_different_transaction_type_never_clusters():
    listings = {
        "a": make_row("a", transaction_type="prodej"),
        "b": make_row("b", transaction_type="pronajem"),
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] == ""


def test_far_apart_listings_do_not_cluster():
    listings = {
        "a": make_row("a", lat=50.0755, lon=14.4378),
        "b": make_row("b", lat=50.2000, lon=14.7000),  # far outside medium threshold
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] == ""
    assert listings["b"]["cluster_id"] == ""


def test_singleton_stays_unclustered():
    listings = {"a": make_row("a")}
    cluster(listings)
    assert listings["a"]["cluster_id"] == ""
    assert listings["a"]["dedup_confidence"] == ""


def test_existing_cluster_id_is_reused_not_regenerated():
    listings = {
        "a": make_row("a", cluster_id="clu_existing123"),
        "b": make_row("b", cluster_id="clu_existing123"),
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] == "clu_existing123"
    assert listings["b"]["cluster_id"] == "clu_existing123"


def test_a_chain_of_matches_is_not_a_cluster():
    """a<->b are ~111 m apart and b<->c ~111 m apart, but a<->c are ~222 m
    apart and do not match at all.

    This used to merge all three through b, and that transitive chaining is
    what produced a 46-row "duplicate" of one-bedroom flats on Honzikova
    measuring 25, 29, 30, 40 and 41 m2 - no two of which match each other.
    A cluster claims these rows are the same flat, and a chain cannot
    support that claim, so a and c must not end up together.
    """
    listings = {
        "a": make_row("a", lat=50.0000, lon=14.0000, price=5_000_000),
        "b": make_row("b", lat=50.0010, lon=14.0000, price=5_000_000),
        "c": make_row("c", lat=50.0020, lon=14.0000, price=5_000_000),
    }
    cluster(listings)
    assert listings["a"]["cluster_id"] != listings["c"]["cluster_id"] or not listings["a"]["cluster_id"]
    # b matched both directly, so it is clustered with one of them.
    assert listings["b"]["cluster_id"]


def test_relisting_finds_removed_candidate_within_lookback_window():
    removed = make_row(
        "old",
        status="removed",
        last_seen_at="2026-01-01T00:00:00+00:00",
        area_m2=55.0,
    )
    new_listing = make_row(
        "new",
        status="active",
        area_m2=55.3,
    )
    new_listing["first_seen_at"] = "2026-03-01T00:00:00+00:00"
    candidate = find_relist_candidate(new_listing, [removed], "2026-03-01T00:00:00+00:00")
    assert candidate == "old"


def test_relisting_ignores_candidates_outside_lookback_window():
    removed = make_row("old", status="removed", last_seen_at="2020-01-01T00:00:00+00:00")
    new_listing = make_row("new")
    candidate = find_relist_candidate(new_listing, [removed], "2026-03-01T00:00:00+00:00")
    assert candidate is None


def test_relisting_ignores_still_active_listings():
    still_active = make_row("still_active", status="active", last_seen_at="2026-01-01T00:00:00+00:00")
    new_listing = make_row("new")
    candidate = find_relist_candidate(new_listing, [still_active], "2026-03-01T00:00:00+00:00")
    assert candidate is None


def test_relisting_requires_removal_before_new_ad_appeared():
    removed_later = make_row("old", status="removed", last_seen_at="2026-04-01T00:00:00+00:00")
    new_listing = make_row("new")
    candidate = find_relist_candidate(new_listing, [removed_later], "2026-03-01T00:00:00+00:00")
    assert candidate is None
