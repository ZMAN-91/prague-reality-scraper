"""The daily city-wide sreality walk as an observer of what is already stored.

sreality is COLLECTED for two boroughs and nowhere else. But rows from a
retired city-wide rent pass are stored outside that belt, and once the hourly
pass narrowed, nothing looked for them: 1,127 adverts sat "active" with no
observer, and the run that wrongly believed it had walked the city marked
1,201 of them missing instead. Both are the same hole.

The donor walk already reads the whole city index every morning to lend
coordinates to iDNES. These tests pin the two halves of using it:

  * it may declare absence (it covers okres Praha), and a two-borough walk
    still may not;
  * it may NOT create rows, or the collection area silently becomes the city.
"""

from unittest.mock import patch

from common import net
from common.schema import (NormalizedListing, STATUS_ACTIVE, STATUS_REMOVED,
                           make_internal_id)
from run import merge_source
from scrapers import sreality
from tools import lend_gps_from_sreality as lend

SOURCE = "sreality"
SCOPE = ("byt", "prodej")
NOW = "2026-09-22T09:00:00+00:00"


def stored(source_id, status=STATUS_ACTIVE, obec="Praha", last_seen="2026-09-20"):
    return {
        "internal_id": make_internal_id(SOURCE, source_id),
        "source": SOURCE,
        "source_id": source_id,
        "url": f"https://www.sreality.cz/detail/{source_id}",
        "property_type": "byt",
        "transaction_type": "prodej",
        "disposition": "2+kk",
        "area_m2": "55",
        "floor": "",
        "lat": "50.05",
        "lon": "14.45",
        "gps_zdroj": "own",
        "address": f"Ulice 1, {obec}",
        "ulice": "Ulice",
        "mestska_cast": "",
        "obec": obec,
        "priority_zone": "False",
        "description": "",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "last_seen_at": last_seen,
        "status": status,
        "cluster_id": "",
        "dedup_confidence": "",
        "relisted_from": "",
    }


def dataset(*source_ids, **kwargs):
    return {make_internal_id(SOURCE, sid): stored(sid, **kwargs)
            for sid in source_ids}


def walked(source_id, price=6_000_000):
    return NormalizedListing(
        source=SOURCE,
        source_id=source_id,
        url=f"https://www.sreality.cz/detail/{source_id}",
        property_type="byt",
        transaction_type="prodej",
        disposition="2+kk",
        area_m2=55.0,
        lat=50.05,
        lon=14.45,
        address="Ulice 1, Praha",
        price=price,
    )


# --- which walk is allowed to say "gone" ------------------------------------

def test_the_city_walk_may_declare_absence_even_though_it_names_its_district():
    """The donor walk asks for okres Praha by name.

    The guard used to read `districts is None` - "did the caller name any
    districts" rather than "which ones" - so the one walk that genuinely
    covers every stored row was the one walk forbidden to say anything about
    them, while the two-borough walk that named nothing was allowed to.
    """
    payload = {"pagination": {"total": 0, "limit": 500}, "results": []}
    with patch.object(net, "fetch_json", return_value=payload), \
         patch.object(net, "polite_sleep", lambda *a, **k: None):
        _l, _p, errors, scopes = sreality.fetch_all(
            object(), districts=(sreality.DISTRICT_PRAHA,))
    assert not errors
    assert SCOPE in scopes, "the city walk still cannot mark absence"


def test_the_two_borough_walk_still_may_not():
    """The regression that cost 1,201 listings. Unchanged by the above."""
    payload = {"pagination": {"total": 0, "limit": 500}, "results": []}
    with patch.object(net, "fetch_json", return_value=payload), \
         patch.object(net, "polite_sleep", lambda *a, **k: None):
        _l, _p, errors, scopes = sreality.fetch_all(
            object(), districts=sreality.AREA_DISTRICT_IDS)
    assert not errors
    assert scopes == set(), f"a two-borough walk claimed {scopes} complete"


def test_the_surrounding_okres_alone_may_not():
    """Praha-vychod is not Prague. Covering it says nothing about a flat in
    Vrsovice, and a guard that tested "any district at all" would let it."""
    payload = {"pagination": {"total": 0, "limit": 500}, "results": []}
    with patch.object(net, "fetch_json", return_value=payload), \
         patch.object(net, "polite_sleep", lambda *a, **k: None):
        _l, _p, _e, scopes = sreality.fetch_all(
            object(), districts=(sreality.DISTRICT_PRAHA_VYCHOD,))
    assert scopes == set()


# --- observing without collecting -------------------------------------------

def test_an_advert_the_walk_sees_but_nothing_stores_is_not_created():
    """The whole point of insert_new=False. sreality is collected for the
    watched belt; if the donor walk could add rows, one morning's coordinate
    errand would quietly replace that decision with the whole city."""
    listings = dataset("kept")
    merge_source(SOURCE, [walked("kept"), walked("stranger")], [], listings,
                 {}, NOW, [], completed_scopes={SCOPE}, insert_new=False)
    assert set(listings) == {make_internal_id(SOURCE, "kept")}


def test_the_same_walk_with_insert_new_would_create_it():
    """Proves the previous test measures the flag and not something else."""
    listings = dataset("kept")
    merge_source(SOURCE, [walked("kept"), walked("stranger")], [], listings,
                 {}, NOW, [], completed_scopes={SCOPE}, insert_new=True)
    assert make_internal_id(SOURCE, "stranger") in listings


def test_a_stored_row_the_walk_finds_is_refreshed():
    listings = dataset("kept")
    merge_source(SOURCE, [walked("kept")], [], listings, {}, NOW, [],
                 completed_scopes={SCOPE}, insert_new=False)
    row = listings[make_internal_id(SOURCE, "kept")]
    assert row["status"] == STATUS_ACTIVE
    assert row["last_seen_at"] == "2026-09-22"


def test_a_stored_row_the_walk_finds_again_comes_back():
    listings = dataset("back", status="missing_2")
    merge_source(SOURCE, [walked("back")], [], listings, {}, NOW, [],
                 completed_scopes={SCOPE}, insert_new=False)
    assert listings[make_internal_id(SOURCE, "back")]["status"] == STATUS_ACTIVE


def test_a_stored_row_the_walk_does_not_find_is_marked_missing():
    """The other half: without this the 1,127 rows are observed in one
    direction only and never leave 'active' however long they are gone."""
    listings = dataset(*[f"live{n}" for n in range(40)])
    listings.update(dataset("gone"))
    merge_source(SOURCE, [walked(f"live{n}") for n in range(40)], [],
                 listings, {}, NOW, [], completed_scopes={SCOPE},
                 insert_new=False)
    assert listings[make_internal_id(SOURCE, "gone")]["status"] != STATUS_ACTIVE


def test_the_drop_guard_counts_the_same_population_on_both_sides():
    """insert_new=False drops unstored adverts BEFORE anything is counted.

    If it dropped them afterwards, a city walk returning thousands of
    strangers would look like a healthy count against a handful of stored
    rows and the guard would never fire; if it counted stored rows against
    a city-sized 'previous', it would fire every time.
    """
    listings = dataset(*[f"id{n}" for n in range(40)])
    strangers = [walked(f"stranger{n}") for n in range(5000)]
    stats = merge_source(SOURCE, strangers + [walked("id0")], [], listings,
                         {}, NOW, [], completed_scopes={SCOPE},
                         insert_new=False)[0]
    assert stats["scopes_absence_marked"] == [], (
        "40 stored rows collapsing to 1 is a broken request, not a market "
        "event - the strangers must not disguise it")


# --- the geographic claim is checked, not assumed ---------------------------

def test_a_stored_row_outside_prague_stops_absence_marking():
    """The walk covers okres Praha. That is enough only while everything
    stored is in Praha - true today, and not something this code controls."""
    listings = dataset("kept")
    listings.update(dataset("ricany", obec="Ricany"))
    stats, _obs, _ch = lend.observe(
        listings, [walked("kept")], [], {SCOPE}, {}, NOW)
    assert listings[make_internal_id(SOURCE, "ricany")]["status"] == STATUS_ACTIVE
    assert stats["scopes_absence_marked"] == []
    assert any("outside Praha" in e for e in stats["errors"])


def test_an_all_prague_store_absence_marks_normally():
    """The stray check must not be permanently on."""
    listings = dataset(*[f"id{n}" for n in range(40)])
    listings.update(dataset("gone"))
    stats, _obs, _ch = lend.observe(
        listings, [walked(f"id{n}") for n in range(40)], [], {SCOPE}, {}, NOW)
    assert stats["scopes_absence_marked"] == ["byt/prodej"]
    assert listings[make_internal_id(SOURCE, "gone")]["status"] != STATUS_ACTIVE


# --- the wiring -------------------------------------------------------------

def test_main_observes_and_writes_what_it_observed(tmp_path):
    """Seven unit tests once passed with the call removed from run(). The
    walk can be perfect and never reach listings.csv."""
    from common import storage

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    listings = dataset(*[f"id{n}" for n in range(40)])
    listings.update(dataset("gone"))
    storage.write_listings(listings, data_dir / "listings.csv")

    seen = [walked(f"id{n}") for n in range(40)]
    with patch.object(lend, "walk_city",
                      return_value=(seen, [], {SCOPE})), \
         patch.object(lend.net, "build_session", lambda *a, **k: object()):
        code = lend.main(["--data-dir", str(data_dir), "--apply"])

    assert code == 0
    written = storage.read_listings(data_dir / "listings.csv")
    assert written[make_internal_id(SOURCE, "gone")]["status"] != STATUS_ACTIVE
    fresh = written[make_internal_id(SOURCE, "id0")]
    assert fresh["status"] == STATUS_ACTIVE
    assert fresh["last_seen_at"] > "2026-09-20", (
        "a row the walk saw kept its old last_seen_at")
    assert list((data_dir / "observations").glob("*.csv")), \
        "the observations the walk produced were dropped"


def test_a_dry_run_writes_nothing(tmp_path):
    from common import storage

    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    listings = dataset(*[f"id{n}" for n in range(40)])
    listings.update(dataset("gone"))
    storage.write_listings(listings, data_dir / "listings.csv")

    seen = [walked(f"id{n}") for n in range(40)]
    with patch.object(lend, "walk_city",
                      return_value=(seen, [], {SCOPE})), \
         patch.object(lend.net, "build_session", lambda *a, **k: object()):
        lend.main(["--data-dir", str(data_dir)])

    written = storage.read_listings(data_dir / "listings.csv")
    assert written[make_internal_id(SOURCE, "gone")]["status"] == STATUS_ACTIVE
    assert not list((data_dir / "observations").glob("*.csv"))
