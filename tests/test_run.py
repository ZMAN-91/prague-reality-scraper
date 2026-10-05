"""Tests for run.merge_source: the state machine that turns a batch of
freshly-scraped listings into listings.csv/observations updates. No network
involved - listings are constructed by hand to simulate what a scraper
module would have returned.
"""

from common.schema import STATUS_ACTIVE, STATUS_REMOVED, NormalizedListing, day_of
from run import merge_source

# Every (property_type, transaction_type) pair the fixtures below use. Real
# scrapers return only the scopes they actually walked to completion; these
# tests pass this explicitly so "did absence-marking run?" is always a
# deliberate choice rather than an accident.
ALL_SCOPES = {("byt", "prodej"), ("byt", "pronajem"), ("dum", "prodej"), ("dum", "pronajem")}
BYT_PRODEJ = {("byt", "prodej")}


def make_listing(
    source_id,
    price=5_000_000,
    area_m2=55.0,
    in_target_area=True,
    property_type="byt",
    transaction_type="prodej",
):
    listing = NormalizedListing(
        source="sreality",
        source_id=source_id,
        url=f"https://example.com/{source_id}",
        property_type=property_type,
        transaction_type=transaction_type,
        disposition="2+kk",
        area_m2=area_m2,
        floor=3,
        lat=50.0755,
        lon=14.4378,
        address="Praha 10",
        description="popis",
        price=price,
    )
    listing.in_target_area = in_target_area
    listing.priority_zone = False
    return listing


def merge(normalized, listings, last_obs, now_iso, errors=None, scopes=ALL_SCOPES, new_ids=None):
    """(stats, observations). Attribute changes are a third return value that
    these tests do not examine - tests/test_changes.py does."""
    stats, observations, _changes = merge_source(
        "sreality",
        normalized,
        errors or [],
        listings,
        last_obs,
        now_iso,
        new_ids if new_ids is not None else [],
        scopes,
    )
    return stats, observations


# --- first sighting / updates ------------------------------------------


def test_new_listing_creates_row_and_one_observation():
    listings, last_obs = {}, {}
    stats, obs = merge([make_listing("1")], listings, last_obs, "2026-03-01T10:00:00+00:00")

    assert stats["new"] == 1
    assert len(obs) == 1
    assert obs[0]["status"] == STATUS_ACTIVE
    row = next(iter(listings.values()))
    assert row["status"] == STATUS_ACTIVE
    assert row["first_seen_at"] == "2026-03-01T10:00:00+00:00"
    assert row["last_seen_at"] == "2026-03-01"  # day-granular, see README


def test_new_listing_row_has_every_schema_field():
    """A row built here is written straight to listings.csv, so a missing
    key would become a silently blank column."""
    from common.schema import LISTING_FIELDS

    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T10:00:00+00:00")
    row = next(iter(listings.values()))
    assert set(row) == set(LISTING_FIELDS)


def test_unchanged_reappearance_produces_no_new_observation():
    listings, last_obs = {}, {}
    merge([make_listing("1", price=5_000_000)], listings, last_obs, "2026-03-01T10:00:00+00:00")
    stats, obs = merge([make_listing("1", price=5_000_000)], listings, last_obs, "2026-03-01T11:00:00+00:00")

    assert stats["updated"] == 1
    assert obs == []  # nothing changed -> no observation row


def test_same_day_reappearance_does_not_touch_the_row_at_all():
    """The whole point of day-granular last_seen_at: an hourly run where
    nothing changed must leave listings.csv byte-identical, otherwise git
    stores a fresh copy of the entire file 24 times a day."""
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T10:00:00+00:00")
    before = dict(next(iter(listings.values())))

    # 22:00 UTC, not 23:59: in Prague that is still the 1st in winter, while
    # 23:59 UTC is already the 2nd and would legitimately bump last_seen_at.
    merge([make_listing("1")], listings, last_obs, "2026-03-01T22:00:00+00:00")
    after = next(iter(listings.values()))
    assert after == before


def test_next_day_reappearance_bumps_last_seen_at_by_one_day():
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T10:00:00+00:00")
    merge([make_listing("1")], listings, last_obs, "2026-03-02T10:00:00+00:00")
    row = next(iter(listings.values()))
    assert row["last_seen_at"] == "2026-03-02"
    assert row["first_seen_at"] == "2026-03-01T10:00:00+00:00"  # untouched


def test_price_change_produces_new_observation_with_price_per_m2():
    listings, last_obs = {}, {}
    merge([make_listing("1", price=5_000_000, area_m2=50.0)], listings, last_obs, "2026-03-01T10:00:00+00:00")
    stats, obs = merge(
        [make_listing("1", price=4_800_000, area_m2=50.0)], listings, last_obs, "2026-03-02T10:00:00+00:00"
    )

    assert len(obs) == 1
    assert obs[0]["price"] == 4_800_000
    assert obs[0]["price_per_m2"] == 96_000


def test_blank_field_does_not_clobber_previously_known_value():
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T10:00:00+00:00")

    thin = make_listing("1")
    thin.disposition = None
    thin.area_m2 = None
    thin.floor = None
    thin.description = None
    merge([thin], listings, last_obs, "2026-03-02T10:00:00+00:00")

    row = next(iter(listings.values()))
    assert row["disposition"] == "2+kk"
    assert row["area_m2"] == 55.0
    assert row["floor"] == 3
    assert row["description"] == "popis"


def test_out_of_area_listing_is_never_stored():
    listings, last_obs = {}, {}
    stats, obs = merge([make_listing("1", in_target_area=False)], listings, last_obs, "2026-03-01T10:00:00+00:00")
    assert listings == {}
    assert obs == []
    assert stats["skipped_out_of_area"] == 1


# --- disappearance / removal -------------------------------------------


def test_a_listing_is_removed_only_after_removal_after_days_of_absence():
    """Two days of being missed is not a removal; the third is.

    The rule this replaces was three consecutive MISSES, which at an hourly
    cadence meant three hours - short enough that a portal hiccup lasting a
    morning produced a wave of false removals. Days, not misses, is what
    makes three safe: it is the same three days for a source walked hourly
    and one whose sweep spans a day.

    It was seven days until the history sheet showed all 9,715 episodes as
    active, because nothing had had time to be confirmed gone. The caution
    moved rather than went: those days are now visible as `disappearing`
    with a count, instead of being indistinguishable from a live advert.
    """
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")

    statuses = []
    for day in range(2, 10):
        merge([], listings, last_obs, f"2026-03-{day:02d}T00:00:00+00:00")
        statuses.append(list(listings.values())[0]["status"])

    from common.schema import REMOVAL_AFTER_DAYS
    n = REMOVAL_AFTER_DAYS
    assert statuses[:n - 1] == [f"missing_{d}" for d in range(1, n)], \
        "something was removed before the days were up"
    assert statuses[n - 1] == STATUS_REMOVED, f"{n} days absent must be removed"


def test_each_missing_step_writes_exactly_one_observation():
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")
    rows = []
    for day in range(2, 10):
        _, obs = merge([], listings, last_obs, f"2026-03-{day:02d}T00:00:00+00:00")
        rows.extend(obs)
    from common.schema import REMOVAL_AFTER_DAYS
    assert [r["status"] for r in rows] == \
        [f"missing_{d}" for d in range(1, REMOVAL_AFTER_DAYS)] + [STATUS_REMOVED]
    assert all(r["price"] == "" for r in rows)  # no price info at a disappearance


def test_removed_listing_is_not_re_marked_forever():
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")
    for day in range(2, 20):
        stats, obs = merge([], listings, last_obs, f"2026-03-{day:02d}T00:00:00+00:00")
    # Once removed, further absent runs must be no-ops (no churn, no rows).
    assert stats["missing_marked"] == 0
    assert obs == []


def test_reappearance_after_missing_resets_to_active():
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")
    merge([], listings, last_obs, "2026-03-02T00:00:00+00:00")  # -> missing_1

    stats, obs = merge([make_listing("1")], listings, last_obs, "2026-03-03T00:00:00+00:00")
    row = list(listings.values())[0]
    assert row["status"] == STATUS_ACTIVE
    assert stats["reactivated"] == 1
    assert len(obs) == 1


def test_reappearance_after_removed_reactivates_the_same_row():
    """Same source_id coming back is a reactivation, not a re-listing -
    relisted_from is only ever for a genuinely new source_id."""
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")
    for day in range(2, 11):
        merge([], listings, last_obs, f"2026-03-{day:02d}T00:00:00+00:00")
    assert list(listings.values())[0]["status"] == STATUS_REMOVED

    merge([make_listing("1")], listings, last_obs, "2026-04-01T00:00:00+00:00")
    assert len(listings) == 1
    row = list(listings.values())[0]
    assert row["status"] == STATUS_ACTIVE
    assert row["relisted_from"] == ""


# --- the guards that stop a bad run looking like a market crash --------


def test_absence_marking_skipped_on_suspicious_drop():
    listings, last_obs = {}, {}
    many = [make_listing(str(i)) for i in range(30)]
    merge(many, listings, last_obs, "2026-03-01T00:00:00+00:00")

    stats, obs = merge(many[:2], listings, last_obs, "2026-03-02T00:00:00+00:00")

    assert stats["run_complete"] is False
    still_active = [r for r in listings.values() if r["status"] == STATUS_ACTIVE]
    assert len(still_active) == 30  # nothing marked missing


def test_suspicious_drop_is_scoped_and_does_not_punish_healthy_categories():
    """A collapse in one category must not freeze removal detection for the
    others - that was the practical effect of the original per-source rule."""
    listings, last_obs = {}, {}
    flats = [make_listing(f"b{i}", property_type="byt") for i in range(30)]
    houses = [make_listing(f"h{i}", property_type="dum") for i in range(30)]
    merge(flats + houses, listings, last_obs, "2026-03-01T00:00:00+00:00")

    # Flats collapse to almost nothing (a broken request); houses look
    # normal with a single listing gone, which is an ordinary sale.
    stats, _ = merge(flats[:1] + houses[:-1], listings, last_obs, "2026-03-02T00:00:00+00:00")

    flat_statuses = {r["status"] for r in listings.values() if r["property_type"] == "byt"}
    house_statuses = sorted(
        {r["status"] for r in listings.values() if r["property_type"] == "dum"}
    )
    assert flat_statuses == {STATUS_ACTIVE}  # every flat protected by the guard
    assert house_statuses == [STATUS_ACTIVE, "missing_1"]  # healthy scope still progresses
    assert listings[[k for k, r in listings.items() if r["source_id"] == "h29"][0]]["status"] == "missing_1"


def test_incomplete_scope_never_marks_absence():
    """The core fix for 'one broken district disables removal detection for
    the whole source forever'."""
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")

    stats, obs = merge([], listings, last_obs, "2026-03-02T00:00:00+00:00", scopes=set())
    assert stats["missing_marked"] == 0
    assert list(listings.values())[0]["status"] == STATUS_ACTIVE
    assert obs == []


def test_only_the_completed_scope_is_absence_marked():
    listings, last_obs = {}, {}
    flat = make_listing("b1", property_type="byt", transaction_type="prodej")
    house = make_listing("h1", property_type="dum", transaction_type="prodej")
    merge([flat, house], listings, last_obs, "2026-03-01T00:00:00+00:00")

    merge([], listings, last_obs, "2026-03-02T00:00:00+00:00", scopes=BYT_PRODEJ)

    by_type = {r["property_type"]: r["status"] for r in listings.values()}
    assert by_type["byt"] == "missing_1"
    assert by_type["dum"] == STATUS_ACTIVE


def test_errors_do_not_by_themselves_freeze_a_completed_scope():
    """An error in one slice is reported, but a scope the scraper still
    walked end-to-end keeps working."""
    listings, last_obs = {}, {}
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00")

    stats, _ = merge(
        [], listings, last_obs, "2026-03-02T00:00:00+00:00",
        errors=["some other district blew up"], scopes=BYT_PRODEJ,
    )
    assert list(listings.values())[0]["status"] == "missing_1"
    assert stats["errors"]  # still surfaced in the run log / exit code


def test_new_internal_ids_are_tracked_for_relisting_pass():
    listings, last_obs = {}, {}
    new_ids = []
    merge([make_listing("1")], listings, last_obs, "2026-03-01T00:00:00+00:00", new_ids=new_ids)
    assert len(new_ids) == 1
    assert new_ids[0] in listings


def test_day_of_matches_stored_last_seen_at():
    """Prague's day, so 23:59 UTC on the 1st is the 2nd - what a person here
    would call it, and what listings.csv now stores."""
    assert day_of("2026-03-01T23:59:59+00:00") == "2026-03-02"


# --- the order sources run in --------------------------------------------


def test_sources_run_in_the_order_they_are_given():
    """Order is the budget policy: whichever source runs last is the one cut
    short when the hour ends, so it must be the largest - bezrealitky at
    ~1730 pages, not sreality at ~35."""
    import run as run_module

    captured = []

    def spy(name):
        def fetcher(session, listings, budget, now=None, *args):
            captured.append(name)
            return [], [], [], set()
        return fetcher

    original = dict(run_module.SOURCE_FETCHERS)
    run_module.SOURCE_FETCHERS.update({name: spy(name) for name in original})
    try:
        order = run_module.parse_sources("sreality,idnes,bezrealitky")
        for name in order:
            run_module.SOURCE_FETCHERS[name](None, {}, None, None)
    finally:
        run_module.SOURCE_FETCHERS.clear()
        run_module.SOURCE_FETCHERS.update(original)

    assert captured == ["sreality", "idnes", "bezrealitky"]


def test_the_default_source_order_puts_the_biggest_source_last():
    import argparse
    import run as run_module

    parser = argparse.ArgumentParser()
    # Mirrors the real default rather than asserting on a literal elsewhere.
    import inspect
    source = inspect.getsource(run_module)
    assert 'default="sreality,idnes,bezrealitky"' in source


# --- "price on request" ------------------------------------------------------


def test_a_placeholder_price_is_not_recorded_as_a_price():
    """sreality publishes "price on request" as 1 Kc. Recorded as a price it
    became a -100 % discount in the weekly report and a pairing key."""
    listings, last_obs = {}, {}
    merge([make_listing("1", price=5_000_000)], listings, last_obs, "2026-10-01T00:00:00+00:00")
    stats, obs = merge([make_listing("1", price=1)], listings, last_obs, "2026-10-02T00:00:00+00:00")

    assert obs == [], "no price is not a price change"
    assert list(last_obs.values())[0]["price"] == 5_000_000, "the last real price stands"


def test_a_placeholder_remembered_from_before_is_dropped():
    listings, last_obs = {}, {}
    merge([make_listing("1", price=1)], listings, last_obs, "2026-10-01T00:00:00+00:00")
    assert list(last_obs.values())[0]["price"] is None
    internal_id = next(iter(last_obs))
    last_obs[internal_id]["price"] = 1  # as stored before 2026-10-05
    merge([make_listing("1", price=1)], listings, last_obs, "2026-10-02T00:00:00+00:00")
    assert last_obs[internal_id]["price"] is None


def test_a_stored_advert_found_excluded_leaves_the_dataset_not_the_market(monkeypatch):
    """A FLATIO let already stored must be removed deliberately, not left to
    go missing and be counted as a departure from a market it was never in."""
    import run as run_module
    from common.budget import Budget
    from scrapers import bezrealitky

    listings = {
        "b1": {"internal_id": "b1", "source": "bezrealitky", "source_id": "1", "url": "u1", "last_seen_at": "2026-10-04"},
        "b2": {"internal_id": "b2", "source": "bezrealitky", "source_id": "2", "url": "u2", "last_seen_at": "2026-10-04"},
        "s1": {"internal_id": "s1", "source": "sreality", "source_id": "1", "url": "x", "last_seen_at": "2026-10-04"},
    }

    def fake_fetch_all(session, budget, excluded=None, **kwargs):
        excluded["1"] = "flatio"
        return [], [], [], set()

    monkeypatch.setattr(bezrealitky, "fetch_all", fake_fetch_all)
    progress = {}
    result = run_module.fetch_bezrealitky(None, listings, Budget(max_seconds=1), None,
                                          ["pronajem"], progress)
    assert len(result) == 5 and result[4] is None
    assert set(listings) == {"b2", "s1"}, "only the bezrealitky advert with that id goes"
    assert progress["bezrealitky"]["vyrazene"] == {"1": "flatio"}, "remembered across runs"


# --- bezrealitky: one read per three-hour cycle ------------------------------


def _bz_row(n, status="active"):
    return {"internal_id": f"b{n}", "source": "bezrealitky", "source_id": str(n),
            "url": f"u{n}", "last_seen_at": "2026-10-05", "status": status}


def _run_bz(monkeypatch, listings, progress, now, pages=()):
    import run as run_module
    from common.budget import Budget
    from scrapers import bezrealitky

    seen = {}

    def fake_fetch_all(session, budget, known_urls=None, due_urls=None,
                       revisit_order=None, excluded=None, **kwargs):
        seen.update(known=set(known_urls), due=set(due_urls),
                    order=sorted(due_urls, key=lambda u: revisit_order.get(u, "")))
        return [], list(pages), [], set()

    monkeypatch.setattr(bezrealitky, "fetch_all", fake_fetch_all)
    run_module.fetch_bezrealitky(None, listings, Budget(max_seconds=1), now,
                                 ["prodej"], progress)
    return seen


def test_an_advert_read_this_cycle_is_not_read_again(monkeypatch):
    """The night pass starts an hour after an area pass has read every
    advert; reading them all again an hour later is what the limit stops."""
    from datetime import datetime, timezone
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    listings = {f"b{n}": _bz_row(n) for n in (1, 2, 3)}
    progress = {"bezrealitky": {"precteno": {
        "1": "2026-10-05T11:00:00+00:00",   # an hour ago: not due
        "2": "2026-10-05T09:00:00+00:00",   # three hours ago: due
    }}}                                      # 3 never read: due
    seen = _run_bz(monkeypatch, listings, progress, now)
    assert seen["known"] == {"u1", "u2", "u3"}, "known still - the sitemap keeps it alive"
    assert seen["due"] == {"u2", "u3"}
    assert seen["order"] == ["u3", "u2"], "longest unread first"


def test_the_limit_is_one_cycle_not_one_in_two():
    """The area pass comes round 170-185 minutes later and reads an advert
    at about the same point each time; a limit at or over that would make
    every other run skip it."""
    import run as run_module
    assert run_module.BEZREALITKY_REREAD_AFTER.total_seconds() / 60 < 170


def test_reads_are_remembered_and_the_memory_stays_the_live_set(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    listings = {"b1": _bz_row(1), "b2": _bz_row(2, status="removed")}
    progress = {"bezrealitky": {"precteno": {
        "1": "2026-10-05T06:00:00+00:00",
        "2": "2026-10-05T06:00:00+00:00",   # removed since: forgotten
        "9": "2026-10-04T06:00:00+00:00",   # no longer stored: forgotten
    }, "vyrazene": {"7": "flatio"}}}
    pages = [{"kind": "index"},
             {"kind": "detail", "url": "u1", "source_id": "1"},
             {"kind": "detail", "url": "u5", "source_id": "5"},   # new, not stored yet
             {"kind": "detail", "url": "u7", "source_id": "7"}]   # excluded
    _run_bz(monkeypatch, listings, progress, now, pages)
    stamp = now.isoformat()
    assert progress["bezrealitky"]["precteno"] == {"1": stamp, "5": stamp}
