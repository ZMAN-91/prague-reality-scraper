"""What stops a cluster from swallowing a whole housing development.

Found by looking at what the scraper had actually produced, not at the code:
a 46-row "duplicate" of one-bedroom flats on Honzikova measuring 25, 29, 30,
40 and 41 m2 - no two of which match each other - and an 18-row cluster
spanning five different streets in Zabehlice, labelled "high" confidence.

A cluster is a claim that these rows are the same flat. Three rules keep that
claim honest: the cluster is a clique, the streets must agree, and the prices
must agree.
"""

from datetime import datetime, timezone

import pytest

from common.dedup import MAX_CLUSTER_SIZE, cluster_listings

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)


def flat(internal_id, *, source="sreality", area=55.0, price=5_000_000,
         address="Nurmiho, Hostivar, Praha", lat="50.04525", lon="14.52430",
         disposition="2+kk"):
    return {
        "internal_id": internal_id, "source": source, "source_id": internal_id,
        "property_type": "byt", "transaction_type": "prodej",
        "disposition": disposition, "area_m2": str(area), "price": price,
        "lat": lat, "lon": lon, "address": address,
        "first_seen_at": "2026-09-10T00:00:00+00:00", "last_seen_at": "2026-09-15",
        "status": "active", "cluster_id": "", "dedup_confidence": "",
    }


def clusters_of(rows):
    cluster_listings(rows, now=NOW)
    groups = {}
    for row in rows.values():
        if row["cluster_id"]:
            groups.setdefault(row["cluster_id"], []).append(row["internal_id"])
    return groups


# --- a cluster is a clique, not a chain and not a star --------------------


def test_a_development_of_similar_flats_is_not_one_duplicate():
    """The Honzikova case, in miniature: one-bedroom flats at 25 to 41 m2,
    each close in size to its neighbour and nothing like the far end."""
    rows = {
        str(area): flat(str(area), area=area, price=2_000_000 + area * 30_000,
                        disposition="1+kk")
        for area in (25, 29, 30, 33, 35, 40, 41, 44)
    }
    groups = clusters_of(rows)
    assert all(len(members) <= 3 for members in groups.values()), groups


def test_a_star_around_one_seed_is_not_a_cluster_either():
    """A seed that matches two listings which do not match each other would
    still put them together - the same chaining, one hop instead of many."""
    rows = {
        "left": flat("left", lat="50.04500", price=5_000_000),
        "middle": flat("middle", lat="50.04600", price=5_000_000),
        "right": flat("right", lat="50.04700", price=5_000_000),
    }
    groups = clusters_of(rows)
    for members in groups.values():
        assert not {"left", "right"} <= set(members), groups


def test_no_cluster_grows_past_the_cap():
    rows = {str(n): flat(str(n)) for n in range(MAX_CLUSTER_SIZE + 6)}
    for members in clusters_of(rows).values():
        assert len(members) <= MAX_CLUSTER_SIZE


# --- the street has to agree ---------------------------------------------


def test_one_estate_is_not_one_flat():
    """Zabehlice produced a single cluster spanning Jasminova, Hvozdikova,
    Jahodova, Pracska and Kapradova. Those are five streets."""
    streets = ["Jasminova", "Hvozdikova", "Jahodova", "Pracska", "Kapradova"]
    rows = {
        name: flat(name, address=f"{name}, Zabehlice, Praha",
                   lat=f"50.0560{n}", lon="14.4990", disposition="2+1", area=55)
        for n, name in enumerate(streets)
    }
    assert clusters_of(rows) == {}, "different streets are different flats"


def test_the_same_street_across_three_portals_still_clusters():
    """The case this is all for: one flat, three agencies, three portals."""
    rows = {
        "s": flat("s", source="sreality", address="Tatarkova, Haje, Praha",
                  area=42, price=6_990_000, lat="50.0335", lon="14.5202"),
        "i": flat("i", source="idnes", address="Tatarkova, Praha 4 - Háje",
                  area=43, price=6_990_000, lat="", lon=""),
        "b": flat("b", source="bezrealitky", address="Tatarkova 729/10, Praha",
                  area=45, price=6_990_000, lat="50.03352", lon="14.52021"),
    }
    groups = clusters_of(rows)
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 3, groups


# --- the price has to agree ----------------------------------------------


def test_two_prices_far_apart_are_two_flats():
    rows = {
        "cheap": flat("cheap", price=2_250_000, area=17, disposition="1+kk"),
        "dear": flat("dear", price=3_890_000, area=18, disposition="1+kk"),
    }
    assert clusters_of(rows) == {}


def test_a_listing_without_a_price_cannot_bridge_two_that_disagree():
    """A priceless seed contradicts nobody, so it used to pull in flats at
    2.2 and 3.9 million and call all three one flat."""
    rows = {
        "cheap": flat("cheap", price=2_250_000, area=17, disposition="1+kk"),
        "unknown": flat("unknown", price=None, area=17, disposition="1+kk"),
        "dear": flat("dear", price=3_890_000, area=18, disposition="1+kk"),
    }
    for members in clusters_of(rows).values():
        assert not {"cheap", "dear"} <= set(members)


def test_the_loosest_tier_needs_a_price_on_both_sides():
    """200 m apart and 15% different in size is where the false merges live,
    so that tier has to be corroborated by something."""
    rows = {
        "a": flat("a", lat="50.04500", area=50, price=None),
        "b": flat("b", lat="50.04600", area=57, price=None),
    }
    assert clusters_of(rows) == {}


def test_a_small_price_difference_is_still_the_same_flat():
    """Agencies do advertise one property at slightly different figures."""
    rows = {
        "a": flat("a", price=6_990_000),
        "b": flat("b", source="idnes", price=6_890_000, lat="", lon="",
                  address="Nurmiho, Praha 15 - Hostivar"),
    }
    assert len(clusters_of(rows)) == 1


# --- a bad cluster must be able to break up ------------------------------


def test_a_cluster_from_an_older_rule_is_not_inherited_forever():
    """Stickiness used to be unconditional, so one bad merge would outlive
    every fix to the matcher."""
    rows = {
        "a": flat("a", price=2_000_000, area=20, disposition="1+kk"),
        "b": flat("b", price=9_000_000, area=90, disposition="4+1"),
    }
    rows["a"]["cluster_id"] = rows["b"]["cluster_id"] = "clu_fromtheoldrule"
    cluster_listings(rows, now=NOW)
    assert rows["a"]["cluster_id"] == "" and rows["b"]["cluster_id"] == ""


def test_two_cliques_that_split_apart_do_not_keep_one_id(monkeypatch):
    """A cluster that breaks in two must not hand both halves its old name.

    Seen live at U zakrutu: clu_e29a carried eight adverts priced 2.25M to
    4.1M, pairs of which directly contradict each other and were never in one
    clique. They were two clusters wearing one id, because each inherited the
    first non-empty cluster_id among its members and that was the same id.

    Downstream nothing can tell them apart, so every analysis that groups by
    cluster_id - which is the one thing cluster_id is for - silently averaged
    two different flats together.
    """
    from common import dedup

    # Two pairs, far enough apart to be separate clusters, each holding one
    # row that carries the old shared id.
    rows = {}
    for internal_id, lat, lon, price, old in (
        ("a1", 50.0400, 14.4800, 3_890_000, "clu_old"),
        ("a2", 50.0400, 14.4800, 3_890_000, ""),
        ("b1", 50.0800, 14.5300, 2_250_000, "clu_old"),
        ("b2", 50.0800, 14.5300, 2_250_000, ""),
    ):
        rows[internal_id] = {
            "internal_id": internal_id, "source": "sreality", "source_id": internal_id,
            "property_type": "byt", "transaction_type": "prodej",
            "disposition": "1+kk", "area_m2": 18.0, "lat": lat, "lon": lon,
            "address": "U zakrutu, Praha", "status": "active",
            "first_seen_at": "2026-09-15T10:00:00+00:00",
            "last_seen_at": "2026-09-16", "cluster_id": old,
            "dedup_confidence": "", "relisted_from": "", "url": f"https://x/{internal_id}",
        }

    dedup.cluster_listings(rows, prices={i: r["area_m2"] and None for i, r in rows.items()}
                           | {"a1": 3_890_000, "a2": 3_890_000,
                              "b1": 2_250_000, "b2": 2_250_000})

    a_id = rows["a1"]["cluster_id"]
    b_id = rows["b1"]["cluster_id"]
    assert a_id and b_id, "both pairs should still cluster"
    assert rows["a2"]["cluster_id"] == a_id
    assert rows["b2"]["cluster_id"] == b_id
    assert a_id != b_id, (
        "two separate clusters were handed the same id, so grouping by "
        "cluster_id merges flats that directly contradict on price"
    )
