"""Lending coordinates between adverts for the same flat."""

import pytest

from common import coords


def row(internal_id, cluster="c1", lat="", lon="", gps_zdroj=""):
    return {"internal_id": internal_id, "cluster_id": cluster,
            "lat": lat, "lon": lon, "gps_zdroj": gps_zdroj}


def test_a_row_without_coordinates_takes_them_from_its_cluster():
    listings = {"a": row("a", lat="50.04", lon="14.48"), "b": row("b")}
    assert coords.lend_within_clusters(listings) == 1
    assert (listings["b"]["lat"], listings["b"]["lon"]) == ("50.04", "14.48")


def test_borrowed_coordinates_say_so():
    """The whole point of the field: a pin on a map drawn from these should
    be able to say which ones are second-hand."""
    listings = {"a": row("a", lat="50.04", lon="14.48"), "b": row("b")}
    coords.lend_within_clusters(listings)
    assert listings["b"]["gps_zdroj"] == "cluster"
    assert listings["a"]["gps_zdroj"] == ""


def test_a_rows_own_coordinates_are_never_overwritten():
    """Two adverts for one flat can disagree, and when they do that is worth
    keeping rather than averaging away."""
    listings = {"a": row("a", lat="50.04", lon="14.48"),
                "b": row("b", lat="50.05", lon="14.49")}
    assert coords.lend_within_clusters(listings) == 0
    assert listings["b"]["lat"] == "50.05"


def test_borrowed_coordinates_are_not_lent_on():
    """Only a portal's own figure may be a donor, or one borrowed reading
    would propagate through a chain of clusters and nothing would record how
    far from the source it had travelled."""
    listings = {"a": row("a", lat="50.04", lon="14.48", gps_zdroj="cluster"),
                "b": row("b")}
    assert coords.lend_within_clusters(listings) == 0
    assert listings["b"]["lat"] == ""


def test_a_cluster_with_no_coordinates_at_all_is_left_alone():
    listings = {"a": row("a"), "b": row("b")}
    assert coords.lend_within_clusters(listings) == 0


def test_unclustered_rows_borrow_nothing():
    """An empty cluster_id is not a cluster - otherwise every GPS-less advert
    in Prague would share one flat's coordinates."""
    listings = {"a": row("a", cluster="", lat="50.04", lon="14.48"),
                "b": row("b", cluster="")}
    assert coords.lend_within_clusters(listings) == 0
    assert listings["b"]["lat"] == ""


def test_one_donor_serves_the_whole_cluster():
    listings = {"a": row("a", lat="50.04", lon="14.48"),
                "b": row("b"), "c": row("c")}
    assert coords.lend_within_clusters(listings) == 2


def test_separate_clusters_do_not_lend_to_each_other():
    listings = {"a": row("a", cluster="c1", lat="50.04", lon="14.48"),
                "b": row("b", cluster="c2")}
    assert coords.lend_within_clusters(listings) == 0


def test_a_half_filled_coordinate_does_not_count_as_having_one():
    listings = {"a": row("a", lat="50.04", lon="14.48"),
                "b": row("b", lat="50.05", lon="")}
    assert coords.lend_within_clusters(listings) == 1
    assert listings["b"]["lon"] == "14.48"
