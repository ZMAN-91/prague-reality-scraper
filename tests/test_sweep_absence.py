"""Absence over a sweep that spans many runs.

The bug this guards against: iDNES rotates through ~257 index pages, so one
run sees perhaps a tenth of Prague's flats. Judging "not seen this run" as
"gone" would mark thousands of live listings missing the moment a rotation
completed. The suspicious-drop guard would then catch that collapse and veto
the scope - every single time - so the source would never record a removal at
all. Both outcomes are wrong; the guard only decided which one.
"""

import pytest

from common.schema import STATUS_ACTIVE, STATUS_REMOVED, make_internal_id
from run import merge_source
from scrapers.idnes import SOURCE_NAME as IDNES


def row(source_id, last_seen, status=STATUS_ACTIVE, source=IDNES):
    return {
        "internal_id": make_internal_id(source, source_id),
        "source": source,
        "source_id": source_id,
        "url": f"https://reality.idnes.cz/detail/prodej/byt/x/{source_id}/",
        "property_type": "byt",
        "transaction_type": "prodej",
        "disposition": "2+kk",
        "area_m2": "55",
        "floor": "",
        "lat": "",
        "lon": "",
        "address": "Ulice, Praha 4",
        "priority_zone": "False",
        "description": "",
        "first_seen_at": "2026-09-01T00:00:00+00:00",
        "last_seen_at": last_seen,
        "status": status,
        "cluster_id": "",
        "dedup_confidence": "",
        "relisted_from": "",
    }


def dataset(seen_days):
    return {make_internal_id(IDNES, sid): row(sid, day) for sid, day in seen_days.items()}


SCOPE = ("byt", "prodej")
NOW = "2026-09-16T09:00:00+00:00"


def test_a_listing_seen_earlier_in_the_sweep_is_not_marked_missing():
    """The core of the fix: one run sees a slice, the sweep sees the whole."""
    listings = dataset({f"id{n}": "2026-09-15" for n in range(50)})
    merge_source(
        IDNES, [], [], listings, {}, NOW, [],
        completed_scopes={SCOPE},
        absence_since={SCOPE: "2026-09-15"},
    )
    assert all(r["status"] == STATUS_ACTIVE for r in listings.values())


def test_a_listing_not_seen_anywhere_in_the_sweep_is_marked_missing():
    listings = dataset(
        {f"live{n}": "2026-09-15" for n in range(40)}
        | {f"gone{n}": "2026-09-10" for n in range(3)}
    )
    merge_source(
        IDNES, [], [], listings, {}, NOW,
        [], completed_scopes={SCOPE}, absence_since={SCOPE: "2026-09-15"},
    )
    statuses = {sid: listings[make_internal_id(IDNES, sid)]["status"] for sid in ("live0", "gone0")}
    assert statuses["live0"] == STATUS_ACTIVE
    assert statuses["gone0"] == "missing_1"


def test_the_suspicious_drop_guard_is_not_tripped_by_a_partial_run():
    """Before the fix this vetoed the scope every time, so nothing was ever
    removed. The guard must measure the sweep, not the run."""
    listings = dataset({f"id{n}": "2026-09-15" for n in range(100)})
    listings[make_internal_id(IDNES, "dead")] = row("dead", "2026-09-01")
    stats, _, _ = merge_source(
        IDNES, [], [], listings, {}, NOW,
        [], completed_scopes={SCOPE}, absence_since={SCOPE: "2026-09-15"},
    )
    assert stats["scopes_absence_marked"] == ["byt/prodej"], \
        "the scope must survive the guard when the sweep really did see everything"
    # Last seen 2026-09-01, fifteen days before NOW, so it is past the week
    # and correctly removed. What this test is about is the line above: that
    # the guard let the scope be marked at all.
    assert listings[make_internal_id(IDNES, "dead")]["status"] == STATUS_REMOVED


def test_a_real_collapse_still_trips_the_guard():
    """The guard must keep working: if the sweep itself saw almost nothing,
    that is a broken source, not a market event."""
    listings = dataset({f"id{n}": "2026-09-01" for n in range(100)})
    stats, _, _ = merge_source(
        IDNES, [], [], listings, {}, NOW,
        [], completed_scopes={SCOPE}, absence_since={SCOPE: "2026-09-15"},
    )
    assert stats["scopes_absence_marked"] == []
    assert all(r["status"] == STATUS_ACTIVE for r in listings.values())


def test_without_a_sweep_window_the_old_per_run_rule_still_applies():
    """sreality and bezrealitky see everything every run, and must keep
    behaving exactly as they did."""
    listings = {
        make_internal_id("sreality", "1"): row("1", "2026-09-15", source="sreality"),
    }
    listings[make_internal_id("sreality", "1")]["source"] = "sreality"
    merge_source(
        "sreality", [], [], listings, {}, NOW, [], completed_scopes={SCOPE}, absence_since=None,
    )
    assert listings[make_internal_id("sreality", "1")]["status"] == "missing_1"


def test_three_sweeps_without_a_sighting_still_reach_removed():
    """The end state must still be reachable - the fix must not make removal
    impossible, which is the failure mode it replaced."""
    listings = dataset({f"id{n}": "2026-09-15" for n in range(60)})
    listings[make_internal_id("idnes", "dead")] = row("dead", "2026-09-01")
    for _ in range(3):
        merge_source(
            IDNES, [], [], listings, {}, NOW,
            [], completed_scopes={SCOPE}, absence_since={SCOPE: "2026-09-15"},
        )
    assert listings[make_internal_id(IDNES, "dead")]["status"] == STATUS_REMOVED
