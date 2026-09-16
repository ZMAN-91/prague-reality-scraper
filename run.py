#!/usr/bin/env python3
"""Main orchestration for one scrape run.

Sale runs hourly (.github/workflows/scrape.yml); rent runs weekly and
city-wide (.github/workflows/scrape-rent.yml). Also runnable locally:

    python run.py                             # sale, the hourly default
    python run.py --transactions pronajem     # rent, the weekly pass
    python run.py --sources sreality          # one source, e.g. while debugging
    python run.py --data-dir /tmp/testdata --logs-dir /tmp/testlogs

Exit code: 0 if every requested source completed cleanly, 1 if any source hit
a real error. All data collected before an error is still written - a partial
run never loses or corrupts anything.

A run that simply ran out of its hour is NOT an error: that is how a sweep
too large to finish in one sitting is meant to end, and the next run picks up
from the stored cursor (common/progress.py, common/interruptions.py). The
distinction is what makes the non-zero exit worth anything - it turns the
Action red, which sends GitHub's built-in "workflow failed" email, and that
email now means something broke rather than that an hour elapsed. Success is
reported separately, by the workflow itself.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common import dedup, interruptions, net, storage
from common.budget import Budget
from common import collection_area, progress as progress_state
from common.geo import is_in_target_area, is_priority_zone
from common.schema import (
    TRANSACTION_TYPES,
    STATUS_ACTIVE,
    STATUS_REMOVED,
    day_of,
    make_internal_id,
    next_missing_status,
    price_per_m2,
    utcnow_iso,
)
from scrapers import bezrealitky, idnes, sreality
from scrapers.idnes import SEARCH_URLS as SEARCH_SCOPES

# A source whose *previous* run had at least this many active listings but
# whose active count drops by more than half this run is treated as
# suspicious (almost certainly a broken request, not a real market crash),
# and we skip absence-marking for it this run rather than trust the data.
# See README "Ochrana proti falešnému hromadnému smazání".
SUSPICIOUS_DROP_MIN_PREV_COUNT = 20
SUSPICIOUS_DROP_RATIO = 0.5


def fetch_sreality(session, listings: dict, budget: Budget, now=None,
                   transactions=None) -> tuple[list, list, list[str], set]:
    """Enrich only what the index walk could not already answer.

    sreality's index rows usually carry GPS, area and disposition outright,
    so most listings need no further request; only a thin row falls back to
    the detail endpoint. Either way this function is the geographic gate for
    anything new from sreality: the index walk deliberately covers whole
    districts, which reach far beyond the target area, so a listing is only
    admitted once real GPS places it inside the bounding box. A new listing
    whose detail fetch fails or is cut off by the budget is skipped this run
    rather than guessed at - it reappears in the next index walk.
    """
    known_ids = {row["source_id"] for row in listings.values() if row["source"] == sreality.SOURCE_NAME}
    normalized, raw_pages, errors, completed_scopes = sreality.fetch_all(
        session, budget, transactions=transactions
    )

    # sreality is collected for one patch of south-east Prague, not for the
    # city (common/collection_area.py). Deciding here rather than at merge
    # time is the whole point: a listing ruled out now costs no detail
    # request, and the previous city-wide behaviour spent its entire detail
    # budget - four hundred requests - on listings that were then discarded.
    #
    # Coordinates decide. An index row without them is worth one request only
    # if its city part or street reads like somewhere in the area; every other
    # unplaced row is dropped unseen. Rows stay in `normalized` with
    # in_target_area=False so merge_source counts them as skipped exactly as
    # it always has.
    # The area decides what is *admitted*, never what is retained. A listing
    # already in the dataset keeps being tracked even if it sits outside -
    # dropping it from this run's results would make merge_source count it as
    # missing and, three runs later, record a removal that never happened.
    # A change of scope is handled by clearing the dataset deliberately, not
    # by quietly inventing removals for everything that fell outside it.
    area_limited = sreality_is_area_limited(transactions)
    worth_placing_ids: set = set()
    for listing in normalized:
        if listing.source_id in known_ids:
            listing.in_target_area = True
            continue
        if not area_limited:
            # A city-wide run (rent, weekly): Prague and its ring, nothing
            # narrower. The watched-area restriction exists to keep an
            # *hourly* sreality sweep small, and that reason does not apply
            # to something that happens once every seven days.
            listing.in_target_area = is_in_target_area(listing.lat, listing.lon)
            if listing.lat is None and listing.address:
                worth_placing_ids.add(listing.source_id)
            continue
        if listing.lat is not None and listing.lon is not None:
            listing.in_target_area = collection_area.contains(listing.lat, listing.lon)
        else:
            listing.in_target_area = False
            # No coordinates. One request is worth spending when the address
            # reads like somewhere in the area - and also when there is no
            # address at all, because "unknown" is not "elsewhere" and
            # silently dropping unplaceable rows would lose real listings
            # without leaving a trace. The detail budget still caps how many
            # of these a single run chases.
            if listing.address is None or collection_area.name_suggests_area(listing.address):
                worth_placing_ids.add(listing.source_id)

    detail_fetches = 0
    for listing in normalized:
        listing.priority_zone = is_priority_zone(listing.lat, listing.lon)
        if listing.source_id in known_ids:
            continue
        if not listing.in_target_area and listing.source_id not in worth_placing_ids:
            # Either placed outside the area, or unplaceable and with nothing
            # in its address to suggest it is worth a request.
            continue
        if listing.extra.get("index_complete"):
            # The index row already carried GPS, area and disposition (and
            # was geo-tagged there), so a detail request would tell us
            # nothing new. Skipping it is the difference between ~30
            # requests and ~12,000 for the same coverage of Prague.
            continue
        if not budget.allow_detail_fetch():
            # Out of budget: leave this listing unenriched, which means
            # unverifiable geography, which means it is skipped this run
            # (see merge_source) and picked up again by a later run. No data
            # is lost - it is still in sreality's index next hour.
            listing.in_target_area = False
            continue
        parsed, raw_detail = sreality.fetch_detail(session, listing.source_id)
        detail_fetches += 1
        net.polite_sleep()
        if raw_detail is not None:
            raw_pages.append(
                {
                    "kind": "detail",
                    "source_id": listing.source_id,
                    "property_type": listing.property_type,
                    "transaction_type": listing.transaction_type,
                    "detail_source_id": listing.source_id,
                    "response": raw_detail,
                }
            )
        if parsed is None:
            listing.in_target_area = False
            continue

        if parsed.get("price") is not None:
            listing.price = parsed["price"]
        listing.area_m2 = parsed.get("area_m2")
        listing.floor = parsed.get("floor")
        listing.disposition = parsed.get("disposition")
        listing.description = parsed.get("description")
        listing.address = parsed.get("address")
        listing.lat = parsed.get("lat")
        listing.lon = parsed.get("lon")
        if parsed.get("url"):
            listing.url = parsed["url"]
        # Now that the detail has given real coordinates, the same rule
        # applies as everywhere else: geography is decided by the point, not
        # by the street name that earned this listing its one request.
        listing.in_target_area = (
            collection_area.contains(listing.lat, listing.lon) if area_limited
            else is_in_target_area(listing.lat, listing.lon)
        )
        listing.priority_zone = is_priority_zone(listing.lat, listing.lon)

    if detail_fetches:
        errors_note = (
            f"sreality: {detail_fetches} listings needed a detail fetch because "
            "their index row lacked GPS/area/disposition"
        )
        print(f"[run] {errors_note}")

    return normalized, raw_pages, errors, completed_scopes


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_bezrealitky(session, listings: dict, budget: Budget, now=None,
                      transactions=None) -> tuple[list, list, list[str], set]:
    """bezrealitky is collected through its published sitemap and the
    listing pages its robots.txt permits (see scrapers/bezrealitky.py), so
    every listing costs one request. The run budget therefore caps how many
    pages a single run visits; already-known listings are queued behind
    fresh ones so new stock is never starved by re-checks.
    """
    # All of Prague, every run: bezrealitky's robots.txt permits this route,
    # so the only thing rationing it is how many listing pages fit in an hour.
    # When an hour cannot cover everything, `revisit_order` makes the
    # leftovers the ones seen most recently, so the rotation comes round
    # evenly rather than re-reading the same head of the list every hour and
    # never reaching the tail.
    known_urls: set[str] = set()
    revisit_order: dict[str, str] = {}
    for row in listings.values():
        if row["source"] != bezrealitky.SOURCE_NAME or not row.get("url"):
            continue
        known_urls.add(row["url"])
        revisit_order[row["url"]] = row.get("last_seen_at") or ""

    return bezrealitky.fetch_all(
        session,
        budget,
        known_urls=known_urls,
        due_urls=known_urls,
        revisit_order=revisit_order,
        # No count cap: the wall-clock budget is the honest limit here, and a
        # separate cap only made the run stop early while time remained.
        max_listings=None,
        transactions=transactions,
    )



def fetch_idnes(session, listings: dict, budget: Budget, now=None,
                transactions=None, progress: Optional[dict] = None):
    """Reality.iDNES.cz: Prague flats, newest first, then the watched area,
    then as much of the index rotation as the hour allows.

    This is the one source whose sweep is deliberately *not* expected to
    finish inside a run. Where it stopped is stored in data/progress.json and
    handed back in on the next run, so an unfinished pass is continued rather
    than restarted - which at one request a second is the difference between
    covering Prague daily and never getting past page 40.
    """
    known = {
        row["source_id"]: row
        for row in listings.values()
        if row["source"] == idnes.SOURCE_NAME and row.get("url")
    }
    entry = (progress or {}).get(idnes.SOURCE_NAME, {})

    # Start the week fresh. The rotation is a cursor that would otherwise
    # wander forward for ever, so each Monday it goes back to page one and
    # the sweep window restarts. A week that did not manage a complete pass
    # simply ends; the next one begins from the front rather than from
    # wherever the last one happened to stop.
    this_week = (now or datetime.now(timezone.utc)).strftime("%G-W%V")
    if entry.get("week") != this_week:
        entry = {"week": this_week}

    cursors = entry.get("page_cursors", {})
    # When the current sweep of each scope began. A scope that has no recorded
    # start has never completed a sweep, so nothing about it can be called
    # absent yet - which is the safe answer, not a missing feature.
    sweep_started = dict(entry.get("sweep_started", {}))
    today = (now or datetime.now(timezone.utc)).date().isoformat()

    normalized, raw_pages, errors, completed, pages, new_cursors = idnes.fetch_all(
        session,
        budget,
        known=known,
        page_cursor=cursors,
        # The whole index, every run. It used to be a twenty-fourth of it
        # per hour, from when sale and rent were collected together and a
        # full pass looked expensive. Sale alone is about 156 pages - some
        # 2.6 minutes at one request a second - against 22 minutes of budget
        # the last run did not use. Reading all of it means every listing's
        # price is refreshed every hour instead of once every half day, and
        # the sweep actually completes, which is what lets a listing that has
        # gone be recognised as gone.
        max_pages=IDNES_PAGES_PER_RUN,
        transactions=transactions,
    )
    # A scope that finished its pass this run gets judged against the day its
    # pass began, and then starts a new pass from today.
    absence_since = {}
    for scope in completed:
        key = f"{scope[0]}/{scope[1]}"
        started = sweep_started.get(key)
        if started:
            absence_since[scope] = started
        sweep_started[key] = today
    for scope in SEARCH_SCOPES:
        if transactions and scope[1] not in set(transactions):
            continue
        sweep_started.setdefault(f"{scope[0]}/{scope[1]}", today)

    if progress is not None:
        progress[idnes.SOURCE_NAME] = entry
        progress_state.note(
            progress, idnes.SOURCE_NAME,
            week=this_week, page_cursors=new_cursors, pages_last_run=pages,
            sweep_started=sweep_started,
        )
    return normalized, raw_pages, errors, completed, absence_since


# Sale and rent are collected on different schedules and at different scope.
#
# Sale runs every hour, and sreality's half of it is restricted to the
# watched area (common/collection_area.py) - that restriction is what makes
# an hourly sreality sweep a small thing rather than a copy of the city.
#
# Rent runs once a week and covers the whole of Prague, so that restriction
# does not apply to it: a weekly city-wide pass is a different trade, and the
# reason for keeping sreality small does not hold when it happens once every
# seven days.
AREA_LIMITED_TRANSACTIONS = {"prodej"}


def sreality_is_area_limited(transactions) -> bool:
    """True when this run's sreality sweep is confined to the watched area."""
    wanted = set(transactions or ())
    return bool(wanted) and wanted <= AREA_LIMITED_TRANSACTIONS


SOURCE_FETCHERS = {
    "sreality": fetch_sreality,
    "bezrealitky": fetch_bezrealitky,
    "idnes": fetch_idnes,
}

# Fetchers that need to remember where they stopped between runs get the
# progress dict passed in; the rest do not need to know it exists.
RESUMABLE_SOURCES = {"idnes"}


def merge_source(
    source_name: str,
    normalized: list,
    errors: list[str],
    listings: dict[str, dict],
    last_obs: dict[str, dict],
    now_iso: str,
    new_internal_ids: list[str],
    completed_scopes: Optional[set] = None,
    absence_since: Optional[dict] = None,
) -> dict:
    """Merge one source's freshly-fetched listings into the shared
    `listings` dict and produce observation rows, in place. Returns a stats
    dict for the run log.

    `completed_scopes` holds the (property_type, transaction_type) pairs the
    scraper walked to completion this run. Absence-marking - the thing that
    eventually flips a listing to `removed` - only runs inside those scopes.
    Passing None means "nothing was completed", the safe default.

    `absence_since` maps a scope to the day its current sweep *started*, for
    a source whose sweep spans many runs. It exists because "not seen in this
    run" is only the same thing as "gone" for a source that sees everything
    every run. iDNES does not: it rotates through ~257 index pages, so one
    run sees perhaps a tenth of Prague. Judging absence per run there would
    have marked thousands of live listings missing the moment a rotation
    completed - and the suspicious-drop guard, catching exactly that, would
    then have discarded the scope every single time, so iDNES would never
    have recorded a removal at all. Either way the data would be wrong; the
    guard only chose which way.

    With `absence_since` set, a listing counts as seen if it was seen at any
    point during the sweep, which is what a multi-run sweep actually knows.

    This scoping matters a lot: sreality alone walks twelve independent
    slices, and the previous "any error anywhere means skip absence-marking
    for the entire source" rule meant one permanently-broken district id
    would silently disable removal detection forever, for every category.
    """

    fetched_by_id = {l.source_id: l for l in normalized if l.in_target_area}
    skipped_out_of_area = sum(1 for l in normalized if not l.in_target_area)
    since_by_scope = dict(absence_since or {})

    def seen_within_sweep(row: dict) -> bool:
        """True if this row was seen during the current sweep of its scope.

        For a single-run source this is never reached with a cutoff set, so
        it reduces to "was it in this run's results" exactly as before.
        """
        since = since_by_scope.get((row["property_type"], row["transaction_type"]))
        if since is None:
            return False
        return (row.get("last_seen_at") or "") >= since

    existing_for_source = {
        row["source_id"]: internal_id
        for internal_id, row in listings.items()
        if row["source"] == source_name
    }

    scopes = set(completed_scopes or set())

    # The suspicious-drop guard, applied per scope rather than per source:
    # a category whose active count collapses is almost certainly a broken
    # request rather than a real market event, so that scope loses its
    # absence-marking permission - without dragging the healthy categories
    # down with it.
    prev_active_by_scope: dict[tuple[str, str], int] = defaultdict(int)
    for internal_id in existing_for_source.values():
        row = listings[internal_id]
        if row["status"] == STATUS_ACTIVE:
            prev_active_by_scope[(row["property_type"], row["transaction_type"])] += 1

    fetched_by_scope: dict[tuple[str, str], int] = defaultdict(int)
    for listing in fetched_by_id.values():
        fetched_by_scope[(listing.property_type, listing.transaction_type)] += 1
    # A multi-run sweep must be measured over the sweep, or the guard sees a
    # 90% "drop" every time a rotation finishes and vetoes it forever.
    for internal_id in existing_for_source.values():
        row = listings[internal_id]
        scope = (row["property_type"], row["transaction_type"])
        if scope in since_by_scope and row["source_id"] not in fetched_by_id and seen_within_sweep(row):
            fetched_by_scope[scope] += 1

    for scope in sorted(scopes):
        prev_active = prev_active_by_scope.get(scope, 0)
        seen_now = fetched_by_scope.get(scope, 0)
        if (
            prev_active >= SUSPICIOUS_DROP_MIN_PREV_COUNT
            and seen_now < prev_active * SUSPICIOUS_DROP_RATIO
        ):
            scopes.discard(scope)
            errors = errors + [
                f"suspicious drop in active listings for {source_name} {scope[0]}/{scope[1]} "
                f"({prev_active} -> {seen_now}); skipping absence-marking for that scope"
            ]

    # A planned stop is not a reason to distrust the data that *was*
    # collected. Whether a scope may be absence-marked is already decided by
    # `completed_scopes`, which a scraper only fills when it walked that
    # scope to its end - so a budget stop keeps its scope out on its own and
    # does not need to poison the others too.
    real_errors, _planned = interruptions.split(errors)

    # A source that returns nothing at all, having run to completion without
    # complaint, is the failure mode this project is most likely to die of:
    # a portal changes a CSS class or a JSON key, the parser quietly matches
    # nothing, every request still returns HTTP 200, and the dataset stops
    # growing without a single error anywhere. The suspicious-drop guard
    # below protects the *stored* data from that, but says nothing to anyone.
    #
    # Only meaningful once there is something to compare against: on the
    # first ever run of a source, zero is simply where it starts.
    if not normalized and not _planned and existing_for_source:
        real_errors = real_errors + [
            f"{source_name} returned no listings at all while {len(existing_for_source)} "
            "are already stored, and reported no error - the parser has most "
            "likely stopped matching the page"
        ]

    run_complete = bool(scopes) and not real_errors

    observation_rows: list[dict] = []
    new_count = 0
    reactivated_count = 0
    updated_count = 0

    def record_observation(internal_id: str, status: str, price, pm2=None) -> None:
        prev = last_obs.get(internal_id)
        changed = prev is None or prev.get("price") != price or prev.get("status") != status
        if not changed:
            return
        observation_rows.append(
            {
                "internal_id": internal_id,
                "observed_at": now_iso,
                "price": price if price is not None else "",
                "price_per_m2": pm2 if pm2 is not None else "",
                "status": status,
            }
        )
        last_obs[internal_id] = {"price": price, "status": status}

    for source_id, listing in fetched_by_id.items():
        internal_id = make_internal_id(source_name, source_id)
        row = listings.get(internal_id)
        pm2 = price_per_m2(listing.price, listing.area_m2)

        if row is None:
            row = {
                "internal_id": internal_id,
                "source": source_name,
                "source_id": source_id,
                "url": listing.url,
                "property_type": listing.property_type,
                "transaction_type": listing.transaction_type,
                "disposition": listing.disposition or "",
                "area_m2": listing.area_m2 if listing.area_m2 is not None else "",
                "floor": listing.floor if listing.floor is not None else "",
                "lat": listing.lat if listing.lat is not None else "",
                "lon": listing.lon if listing.lon is not None else "",
                "address": listing.address or "",
                "priority_zone": listing.priority_zone,
                "description": listing.description or "",
                "first_seen_at": now_iso,
                "last_seen_at": day_of(now_iso),
                "status": STATUS_ACTIVE,
                "cluster_id": "",
                "dedup_confidence": "",
                "relisted_from": "",
            }
            listings[internal_id] = row
            new_internal_ids.append(internal_id)
            new_count += 1
        else:
            if row["status"] != STATUS_ACTIVE:
                reactivated_count += 1
            else:
                updated_count += 1
            # Only overwrite slow-changing fields with a non-empty new
            # value: a field that came back blank this run (a temporary API
            # hiccup on that one field) must not clobber good history.
            if listing.disposition:
                row["disposition"] = listing.disposition
            if listing.area_m2 is not None:
                row["area_m2"] = listing.area_m2
            if listing.floor is not None:
                row["floor"] = listing.floor
            if listing.address:
                row["address"] = listing.address
            if listing.description:
                row["description"] = listing.description
            if listing.lat is not None and listing.lon is not None:
                row["lat"] = listing.lat
                row["lon"] = listing.lon
                row["priority_zone"] = listing.priority_zone
            row["url"] = listing.url or row["url"]
            # Day-granular on purpose: a second-granular value here would
            # rewrite every active row of listings.csv on every hourly run,
            # which destroys git's delta compression and grows the repo by
            # roughly the whole file every hour. See README.
            row["last_seen_at"] = day_of(now_iso)
            row["status"] = STATUS_ACTIVE

        if getattr(listing, "presence_only", False) and row is not None:
            # Proof of life only - no fresh content was read, so there is
            # nothing to observe. Writing a row here would log the price as
            # having vanished.
            continue
        record_observation(internal_id, STATUS_ACTIVE, listing.price, pm2)

    missing_count = 0
    removed_count = 0
    for source_id, internal_id in existing_for_source.items():
        if source_id in fetched_by_id:
            continue
        row = listings[internal_id]
        if row["status"] == STATUS_REMOVED:
            continue
        if (row["property_type"], row["transaction_type"]) not in scopes:
            # This listing's category wasn't fully walked this run, so its
            # absence proves nothing.
            continue
        if seen_within_sweep(row):
            # Seen earlier in this sweep, just not in this particular run.
            continue
        new_status = next_missing_status(row["status"])
        row["status"] = new_status
        missing_count += 1
        if new_status == STATUS_REMOVED:
            removed_count += 1
        record_observation(internal_id, new_status, None)

    return {
        "fetched": len(normalized),
        "skipped_out_of_area": skipped_out_of_area,
        "new": new_count,
        "updated": updated_count,
        "reactivated": reactivated_count,
        "missing_marked": missing_count,
        "removed_confirmed": removed_count,
        "run_complete": run_complete,
        "scopes_absence_marked": sorted("/".join(s) for s in scopes),
        "interruptions": [interruptions.strip_marker(m) for m in _planned],
        "errors": real_errors,
        "observation_rows": len(observation_rows),
    }, observation_rows


DEFAULT_TRANSACTIONS = ("prodej",)

# Index pages iDNES may read in one run, high enough to cover the whole thing
# and still be a cap rather than a target: the walk stops on its own at the
# first empty page, and the run budget can stop it sooner.
IDNES_PAGES_PER_RUN = 400


def run(
    sources: list[str],
    data_dir: Path,
    logs_dir: Path,
    max_seconds: Optional[float] = None,
    max_new_details: Optional[int] = None,
    transactions: Optional[list] = None,
) -> int:
    transactions = list(transactions or DEFAULT_TRANSACTIONS)
    raw_dir = data_dir / "raw"
    listings_path = data_dir / "listings.csv"
    observations_dir = data_dir / "observations"
    state_path = data_dir / "state" / "last_observation.json"

    storage.ensure_dirs(data_dir, logs_dir)

    now = datetime.now(timezone.utc)
    now_iso = utcnow_iso()
    budget = Budget(max_seconds=max_seconds, max_new_details=max_new_details)
    # The budget is checked between listings; this makes it bind inside a
    # single request too, so retries and Retry-After waits cannot carry a run
    # past its own hour. See common/net.set_deadline.
    net.set_deadline(budget.deadline)

    session = net.build_session()
    listings = storage.read_listings(listings_path)
    last_obs = storage.read_last_observation_state(state_path)
    # Where the last run stopped. Read before anything fetches, written after
    # everything has, so a run killed mid-flight leaves the cursor where it
    # was and simply repeats a little rather than skipping a stretch.
    progress = progress_state.read(listings_path.parent)

    run_stats: dict = {
        "started_at": now_iso,
        "transactions": transactions,
        "sreality_area_limited": sreality_is_area_limited(transactions),
        "sources": {},
    }
    new_internal_ids: list[str] = []
    all_observation_rows: list[dict] = []
    any_errors = False

    for source_name in sources:
        fetcher = SOURCE_FETCHERS.get(source_name)
        if fetcher is None:
            print(f"[run] unknown source {source_name!r}, skipping", file=sys.stderr)
            continue

        try:
            absence_since = None
            if source_name in RESUMABLE_SOURCES:
                # These return one extra value: over what window absence may
                # be judged, because their sweep spans several runs.
                normalized, raw_pages, errors, completed_scopes, absence_since = fetcher(
                    session, listings, budget, now, transactions, progress
                )
            else:
                normalized, raw_pages, errors, completed_scopes = fetcher(
                    session, listings, budget, now, transactions
                )
        except Exception:
            # Last-resort safety net: even a bug we didn't anticipate in a
            # scraper must not take down the other source's run.
            tb = traceback.format_exc()
            print(f"[run] {source_name} crashed:\n{tb}", file=sys.stderr)
            run_stats["sources"][source_name] = {"crashed": True, "traceback": tb}
            any_errors = True
            continue

        stats, observation_rows = merge_source(
            source_name, normalized, errors, listings, last_obs, now_iso,
            new_internal_ids, completed_scopes, absence_since,
        )

        try:
            # Each raw page says what it is. Guessing from the presence of a
            # key used to misfile bezrealitky's per-listing pages as "index"
            # dumps, which are throttled to one a day - so its real payloads
            # were silently never archived at all.
            index_pages = [p for p in raw_pages if p.get("kind", "index") == "index"]
            detail_pages = [p for p in raw_pages if p.get("kind") == "detail"]

            # Archive a listing's payload the first time it is seen and again
            # whenever what it says has changed - not on every re-read.
            #
            # The old rule was "details are fetched once per listing ever, so
            # always archive them". That was true of sreality and became false
            # the moment bezrealitky started re-reading all 1730 of its Prague
            # listing pages every hour: 1.6 MB of gzip an hour, which git
            # cannot delta-compress because it is already compressed, is about
            # 14 GB a year of repository that can never be reclaimed, to store
            # the same unchanged pages 8760 times each.
            changed_ids = {row["internal_id"] for row in observation_rows}
            changed_ids.update(new_internal_ids)
            detail_pages = [
                page for page in detail_pages
                if page.get("source_id") is None
                or make_internal_id(source_name, page["source_id"]) in changed_ids
            ]

            # Index dumps are throttled to one per day (they are the whole
            # active market, every hour, and every price change they carry is
            # already in observations/).
            storage.write_raw_archive(
                source_name, now, {"pages": index_pages}, raw_dir,
                kind="index", once_per_day=True,
            )
            if detail_pages:
                storage.write_raw_archive(
                    source_name, now, {"pages": detail_pages}, raw_dir, kind="detail",
                )
            stats["raw_details_archived"] = len(detail_pages)
        except OSError as exc:
            print(f"[run] failed to write raw archive for {source_name}: {exc}", file=sys.stderr)

        run_stats["sources"][source_name] = stats
        all_observation_rows.extend(observation_rows)
        # Planned stops are printed, not raised: they are how a run that
        # cannot finish inside its hour is supposed to end, and treating them
        # as failures made every healthy run send a failure email - which
        # also meant a genuine outage would have looked identical.
        for stop in stats.get("interruptions", []):
            print(f"[run] {source_name} stopped as planned: {stop}")
        if stats["errors"]:
            any_errors = True
            for err in stats["errors"]:
                print(f"[run] {source_name} ERROR: {err}", file=sys.stderr)

    # Re-listing detection: for every genuinely new listing this run, check
    # whether it looks like a re-advertisement of something previously
    # marked removed (on either source). The index is built once per run -
    # the removed pool only ever grows, so a per-listing linear scan over it
    # would get slower every month this project runs.
    if new_internal_ids:
        removed_index = dedup.RemovedIndex(listings.values())
        for internal_id in new_internal_ids:
            new_row = listings[internal_id]
            candidate = dedup.find_relist_candidate(
                new_row, removed_index, new_row["first_seen_at"]
            )
            if candidate and candidate != internal_id:
                new_row["relisted_from"] = candidate

    # The last price seen for each listing, so two flats in the same new
    # development are not called the same flat: geometry cannot tell them
    # apart, and price can.
    dedup.cluster_listings(
        listings,
        prices={i: state.get("price") for i, state in last_obs.items()},
    )

    storage.write_listings(listings, listings_path)
    progress_state.write(listings_path.parent, progress)
    storage.append_observations(all_observation_rows, now, observations_dir)
    storage.write_last_observation_state(last_obs, state_path)

    run_stats["finished_at"] = utcnow_iso()
    run_stats["total_listings"] = len(listings)
    run_stats["total_observations_written"] = len(all_observation_rows)
    run_stats["budget"] = budget.summary()
    run_stats["sreality_collection_area"] = collection_area.describe()
    run_stats["progress"] = progress
    run_stats["ok"] = not any_errors
    storage.write_run_log(run_stats, logs_dir)

    print(
        f"[run] done. listings={len(listings)} new_observations={len(all_observation_rows)} "
        f"ok={not any_errors}"
    )
    return 0 if not any_errors else 1


def parse_transactions(raw: str) -> list[str]:
    """Transaction types to collect, validated.

    A typo here would silently collect nothing at all - the scrapers would
    filter every listing away and the run would look merely empty - so an
    unknown name is refused loudly instead.
    """
    wanted = [part.strip() for part in raw.split(",") if part.strip()]
    unknown = [name for name in wanted if name not in TRANSACTION_TYPES]
    if unknown:
        raise SystemExit(
            f"unknown transaction type(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(TRANSACTION_TYPES))})"
        )
    return wanted or list(DEFAULT_TRANSACTIONS)


def parse_sources(raw: str) -> list[str]:
    """Source names in the order they will run, which is a budget policy.

    Whichever source runs last is the one cut short when the hour ends, so
    the order is deliberate: sreality first (the watched area, ~35 requests),
    then iDNES (a few hundred), then bezrealitky, which at ~1730 listing
    pages is both the largest and the one that loses least by being trimmed -
    its sitemap has already confirmed every listing still exists, so what a
    cut-short sweep costs is a re-read of content, not knowledge of what is
    on the market.

    Duplicates are dropped rather than run twice, keeping first position.
    """
    seen: set = set()
    order: list = []
    for name in (part.strip() for part in raw.split(",")):
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    return order


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources",
        default="sreality,idnes,bezrealitky",
        help="comma-separated sources, run in the order given (see parse_sources)",
    )
    parser.add_argument(
        "--transactions",
        default=",".join(DEFAULT_TRANSACTIONS),
        help=(
            "comma-separated transaction types to collect: prodej, pronajem. "
            "Sale runs hourly and, on sreality, only inside the watched area; "
            "rent runs weekly and covers the whole of Prague."
        ),
    )
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR), help="override data/ directory (mainly for tests)")
    parser.add_argument("--logs-dir", default=str(storage.LOGS_DIR), help="override logs/ directory (mainly for tests)")
    parser.add_argument(
        "--max-seconds",
        type=float,
        default=None,
        help=(
            "wall-clock budget for fetching; the run then stops fetching and "
            "still writes everything it collected. Set it comfortably below "
            "the CI job timeout."
        ),
    )
    parser.add_argument(
        "--max-new-details",
        type=int,
        default=None,
        help=(
            "cap one-time detail fetches for newly-seen listings in this run. "
            "The rest are picked up by later runs, so a large initial backlog "
            "drains over several runs instead of blowing one run's timeout."
        ),
    )
    args = parser.parse_args()

    return run(
        parse_sources(args.sources),
        Path(args.data_dir),
        Path(args.logs_dir),
        max_seconds=args.max_seconds,
        max_new_details=args.max_new_details,
        transactions=parse_transactions(args.transactions),
    )


if __name__ == "__main__":
    sys.exit(main())
