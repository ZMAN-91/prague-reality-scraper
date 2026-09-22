"""Give iDNES listings coordinates by finding the same flat on sreality.

    python -m tools.lend_gps_from_sreality --data-dir store/data [--apply]

iDNES publishes no coordinates at all - not in the search index, not on the
detail page, nowhere - and it is 64% of everything collected here. Without a
coordinate a listing cannot be given a house number, cannot be placed in a
priority zone, and cannot be put on a map. Measured before this existed:
5,175 of 11,025 listings had no coordinates and every single one was from
iDNES.

Those that did have one got it from a cluster sibling: the same flat also
advertised on sreality or bezrealitky, whose coordinates were borrowed. That
worked for 1,864 rows and stopped there for one reason only - the sreality
adverts stored here are limited to the two watched boroughs, so a flat
advertised in Liben had no sibling to borrow from.

This widens the pool of siblings without widening the dataset.

WHAT IT COSTS, AND WHY THAT IS ACCEPTABLE

sreality's search index carries gps_lat and gps_lon on every row, so this
never fetches a detail page. All of Prague is about 25 index requests at 500
rows a page, once a day.

That is a real change of posture and worth stating plainly: this project's
rule had been that nothing walks sreality city-wide, because sreality's
robots.txt disallows everything and the traffic sent there is deliberately
kept near what a person clicking around would produce. The hourly area pass
already sends it roughly 840 requests a day. This adds about 25 - three per
cent - on the cheapest path the API offers, reading the index rather than one
request per listing. The footprint argument that set the original rule is the
same argument that permits this.

THE DONORS ARE NOT COLLECTED - BUT THE WALK IS USED TWICE

No donor becomes a row. Storing them would change what the dataset means:
sreality is collected for the watched area, deliberately, and one morning's
coordinate errand must not quietly replace that with the whole city.

What the walk IS used for, besides lending coordinates, is saying which
sreality adverts already stored here are still up. That is not the same
concession. Absence-marking needs a walk that covers the stored population,
and this one does - okres Praha, every category, every day, and every
sreality row stored here is in Praha (checked each run by `observe`, not
assumed). The hourly pass covers only the two watched boroughs, so without
this the 1,127 rows left outside the belt by the retired city-wide rent pass
had no observer at all: they would have sat "active" for ever, and the run
that wrongly believed a narrowed walk was complete marked 1,201 of them
missing instead. Same hole, two directions.

`run.merge_source(insert_new=False)` is what keeps the two apart: refresh,
reactivate and absence-mark what is stored; drop what is not.

SAMENESS IS DECIDED BY dedup, NOT HERE

common.dedup.best_match is the same scoring that clustering uses. A listing
with no coordinates can reach at most "medium" against one that has them -
street plus disposition plus area plus an agreeing price - which is real
evidence and weaker than two points forty metres apart. gps_zdroj records
"sreality" rather than "cluster" so the two are told apart afterwards: one
came from an advert in this dataset, the other from an advert that was
looked at once and not kept.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import run
from common import cas, dedup, net, price_memory, ruian, storage
from common.budget import Budget
from scrapers import sreality

#: What gps_zdroj says for a coordinate that came from this. Distinct from
#: coords.FROM_CLUSTER: that donor is a row in listings.csv and can be looked
#: at; this one was read from the index and dropped.
FROM_SREALITY = "sreality"

#: How far two stated floor areas may differ and still be one flat, in
#: metres. Absolute, not relative: measured over 2,292 pairs already judged
#: the same flat the difference is 0.0 m2 at the median, and 93.9% agree
#: within a metre. A percentage is the wrong shape - 15% of a 120 m2 flat is
#: 18 m2, which is a different flat, while 15% of a 25 m2 studio is under 4.
AREA_TOLERANCE_M = 1.0

#: Price rules tried in order. Exact first, then a wider one for rows exact
#: found nothing for.
#:
#: Widening it for EVERY row measured worse than exact - 633 pairs against
#: 653, and crowding from 13 up to 36 - because a looser rule turns confident
#: pairs into crowded ones. Applied only to the leftovers it cannot: the
#: pairs already found are never re-examined. Measured on Praha 10 this adds
#: 11 pairs in 787 and moves crowding by one.
#:
#: Ten per cent is a lot of money, and the check that it is not buying wrong
#: answers is the register's own district: over the 11 rows the second stage
#: found, it agreed 100% - no worse than the first stage's 99%. Eleven rows
#: is thin evidence, so the city-wide district agreement printed by
#: tools/backfill_cislo.py (98.4% before this) is what keeps watching it.
PRICE_STAGES = (None, 10.0)


def walk_city(session, budget=None, districts=None):
    """One city-wide read of sreality's index.

    Returns (normalized, errors, completed_scopes) so the same walk can be
    used twice: once to lend coordinates, once to say which of the sreality
    adverts already stored here are still up. Nothing is fetched twice.
    """
    listings, _pages, errors, scopes = sreality.fetch_all(
        session,
        budget=budget,
        districts=districts or (sreality.DISTRICT_PRAHA,),
    )
    return listings, errors, scopes


def donors_from(listings) -> list:
    """The walk's rows, shaped like listings.csv.

    Only rows that carry a coordinate: a donor without one has nothing to
    give, and keeping it would only slow the matching down.
    """
    today = cas.today().isoformat()
    donors = []
    for item in listings:
        if item.lat is None or item.lon is None:
            continue
        donors.append({
            "internal_id": f"sreality-donor-{item.source_id}",
            "source": "sreality",
            "source_id": item.source_id,
            "property_type": item.property_type,
            "transaction_type": item.transaction_type,
            "disposition": item.disposition or "",
            "area_m2": item.area_m2 if item.area_m2 is not None else "",
            "price": item.price if item.price is not None else "",
            "lat": item.lat,
            "lon": item.lon,
            "address": item.address or "",
            # A donor is on the market now. The window has to overlap the
            # listing it lends to, and "now" is the only honest value: the
            # index says the advert is up, not how long it has been up.
            "first_seen_at": today,
            "last_seen_at": today,
            "status": "active",
        })
    return donors


def collect_donors(session, budget=None, districts=None):
    """walk_city + donors_from, for callers that only want the donors."""
    listings, errors, _scopes = walk_city(session, budget=budget,
                                          districts=districts)
    return donors_from(listings), errors


def observe(listings, walked, errors, scopes, last_obs, now_iso):
    """Tell the stored sreality rows apart into still-up and gone.

    THE HOLE THIS CLOSES

    sreality is collected for the two watched boroughs and nowhere else, so
    the hourly pass asks about districts 5004 and 5010 only. But 1,127 rows
    from a retired city-wide rent pass are stored outside that belt, and
    once the hourly pass narrowed, nothing looked for them at all. They
    would have sat "active" for ever, and a narrowed walk that mistakenly
    claimed completeness marked every one of them missing instead - both
    failures of the same missing observer.

    This walk already reads the whole city. Using it costs nothing: the
    rows are in memory either way, and the alternative was one more walk.

    IT OBSERVES, IT DOES NOT COLLECT

    `insert_new=False` - see run.merge_source. A source_id the walk sees
    and this project has never stored is dropped. The collection area is a
    decision, not an accident of which index page was read.
    """
    # The walk covers okres Praha. That is enough only for as long as
    # every stored sreality row is in Praha, which is true today and is
    # nothing this code controls - so it is checked rather than assumed,
    # every run, and a stray row costs absence-marking rather than
    # costing the stray row's neighbours their status.
    strays = sorted({(row.get("obec") or "").strip()
                     for row in listings.values()
                     if row["source"] == "sreality"} - {"Praha", ""})
    if strays:
        errors = list(errors) + [
            "sreality rows stored outside Praha (" + ", ".join(strays[:5])
            + ") - the city walk does not cover them, so absence-marking "
              "is skipped this run"
        ]
        scopes = set()

    new_ids: list[str] = []
    return run.merge_source(
        "sreality",
        walked,
        errors,
        listings,
        last_obs,
        now_iso,
        new_ids,
        completed_scopes=scopes,
        insert_new=False,
    )


def _price_histories(data_dir) -> dict:
    """{internal_id: [every price it was seen at]}, newest first.

    From the observations this project already writes, so the listing side of
    the comparison costs nothing to obtain. Its own latest price is included
    by being the newest observation.
    """
    import csv
    from pathlib import Path

    histories = {}
    folder = Path(data_dir) / "observations"
    for path in sorted(folder.glob("*.csv"), reverse=True):
        try:
            handle = open(path, encoding="utf-8")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                price = (row.get("price") or "").strip()
                if not price:
                    continue
                try:
                    value = float(price)
                except ValueError:
                    continue
                seen = histories.setdefault(row["internal_id"], [])
                if value not in seen:
                    seen.append(value)
    return histories


def _areas_agree(row: dict, candidate: dict) -> bool:
    a = dedup._to_float(row.get("area_m2"))
    b = dedup._to_float(candidate.get("area_m2"))
    return a is not None and b is not None and abs(a - b) <= AREA_TOLERANCE_M


def _why_not(row: dict, candidates: list) -> str:
    """Which requirement the closest candidate fell at.

    Diagnostic only, and it deliberately re-checks rather than asking dedup:
    dedup answers one question - same flat or not - and does not rank its
    reasons. Without this the report says "no confident match" 4,899 times
    and cannot tell "these flats are not advertised on sreality" from "the
    test is too strict", which are opposite conclusions with opposite fixes.

    Reported for the candidate that gets FURTHEST, so the answer is the last
    hurdle rather than the first one some unrelated flat on the street
    tripped over.
    """
    order = ["property_type", "transaction_type", "disposition", "price",
             "area", "locality"]
    best = -1
    for candidate in candidates:
        if row.get("property_type") != candidate.get("property_type"):
            reached = 0
        elif row.get("transaction_type") != candidate.get("transaction_type"):
            reached = 1
        elif not ((row.get("disposition") or "").strip()
                  and (row.get("disposition") or "").strip()
                  == (candidate.get("disposition") or "").strip()):
            reached = 2
        elif not dedup._prices_agree(row, candidate):
            reached = 3
        elif not _areas_agree(row, candidate):
            reached = 4
        else:
            reached = 5
        best = max(best, reached)
    return order[best] if best >= 0 else order[0]


def repair(listings: dict, donors: list, prices: dict = None,
           remembered: dict = None) -> tuple:
    """Re-examine coordinates borrowed earlier. Returns (changed, stats).

    A pair, once made, was never looked at again: `lend` skips any row that
    already has coordinates. That is right for the common case and wrong for
    the failure case. Where exactly one candidate matched and it was the
    wrong flat - the true twin absent from sreality that day, or priced
    differently - the row keeps a wrong coordinate for ever, and a wrong
    coordinate quietly becomes a wrong house number.

    So once a week every borrowed coordinate is matched again against that
    day's adverts, and a different answer is taken as the better one: it was
    reached with more price history and a fuller donor set than the original.

    Only rows that BORROWED from a donor. A portal's own coordinate is never
    touched, and one borrowed from a clustered sibling belongs to dedup.

    A row whose pair has vanished keeps what it has. The advert being gone
    from sreality today is not evidence the coordinate was wrong, and
    throwing it away would lose good data to prove a point.
    """
    stats: Counter = Counter()
    prices = prices or {}
    remembered = remembered or {}

    borrowed = [row for row in listings.values()
                if row.get("gps_zdroj") == FROM_SREALITY]
    stats["borrowed coordinates re-examined"] = len(borrowed)
    if not borrowed or not donors:
        return 0, stats

    by_street = {}
    for donor in donors:
        key = dedup.street_key(donor.get("address"))
        if key:
            by_street.setdefault(key, []).append(donor)

    changed = 0
    for row in borrowed:
        key = dedup.street_key(row.get("address"))
        candidates = by_street.get(key) if key else None
        if not candidates:
            stats["pair no longer on offer - kept"] += 1
            continue

        probe = dict(row)
        probe.pop("lat", None)
        probe.pop("lon", None)
        seen = prices.get(row["internal_id"])
        if isinstance(seen, (list, tuple, set)):
            probe["prices"] = list(seen)
        elif seen is not None:
            probe["price"] = seen

        dated = []
        for candidate in candidates:
            history = price_memory.history_for(
                remembered, candidate.get("source_id"))
            if history:
                candidate = dict(candidate)
                candidate["prices"] = (
                    [candidate["price"]] + history
                    if candidate.get("price") not in (None, "")
                    else history)
            dated.append(candidate)

        found = None
        for price_rel in PRICE_STAGES:
            found = dedup.best_match(probe, dated,
                                     area_abs_m=AREA_TOLERANCE_M,
                                     price_rel_pct=price_rel)
            if found:
                break
        if not found:
            stats["pair no longer on offer - kept"] += 1
            continue

        donor, _confidence = found
        if (str(donor["lat"]) != str(row.get("lat"))
                or str(donor["lon"]) != str(row.get("lon"))):
            row["lat"] = donor["lat"]
            row["lon"] = donor["lon"]
            # The house number was derived from the old coordinate. Clearing
            # the source makes the next backfill redo it rather than leave a
            # number that belongs to a building this row no longer points at.
            for field in ruian.MATCH_FIELDS:
                row[field] = ""
            changed += 1
            stats["coordinate changed"] += 1
        else:
            stats["coordinate confirmed"] += 1

    return changed, stats


def lend(listings: dict, donors: list, prices: dict = None,
         remembered: dict = None) -> tuple:
    """Fill blank coordinates from a matching donor. Returns (filled, stats).

    `prices` maps a listing to every price it has been seen at; `remembered`
    is common.price_memory's record of what the donors cost on recent days.
    Both are histories rather than single figures, because a flat discounted
    on one portal a day before the other has disagreeing prices today and an
    intersecting history.
    """
    remembered = remembered or {}
    stats: Counter = Counter()
    prices = prices or {}

    wanted = []
    for row in listings.values():
        if str(row.get("lat") or "").strip() and str(row.get("lon") or "").strip():
            stats["already had coordinates"] += 1
            continue
        wanted.append(row)

    if not donors:
        stats["no donors were collected"] += len(wanted)
        return 0, stats

    # Bucketed by street, because matching every GPS-less row against every
    # donor is 5,175 x 12,000. A match needs the same street_key on both
    # sides when one side has no coordinates - which is every row here - so
    # a row can only ever match inside its own bucket.
    by_street = {}
    for donor in donors:
        key = dedup.street_key(donor.get("address"))
        if key:
            by_street.setdefault(key, []).append(donor)

    filled = 0
    for row in wanted:
        key = dedup.street_key(row.get("address"))
        if not key:
            stats["no usable street"] += 1
            continue
        candidates = by_street.get(key)
        if not candidates:
            stats["street has no sreality advert"] += 1
            continue

        # The price the row is actually carrying, so the agreement test has
        # something to compare. listings.csv holds no price column; it lives
        # in the observations.
        probe = dict(row)
        seen = prices.get(row["internal_id"])
        if isinstance(seen, (list, tuple, set)):
            probe["prices"] = list(seen)
        elif seen is not None:
            probe["price"] = seen

        # Give each candidate the prices it was seen at on recent days, so
        # an exact match to yesterday's figure counts as the evidence it is.
        dated = []
        for candidate in candidates:
            history = price_memory.history_for(
                remembered, candidate.get("source_id"))
            if history:
                candidate = dict(candidate)
                candidate["prices"] = (
                    [candidate["price"]] + history
                    if candidate.get("price") not in (None, "")
                    else history)
            dated.append(candidate)
        candidates = dated

        found = None
        for stage, price_rel in enumerate(PRICE_STAGES):
            found = dedup.best_match(probe, candidates,
                                     area_abs_m=AREA_TOLERANCE_M,
                                     price_rel_pct=price_rel)
            if found:
                stats[f"matched at stage {stage}"] += 1
                break
        if not found:
            stats["no confident match"] += 1
            stats[f"  why: {_why_not(probe, candidates)}"] += 1
            continue

        donor, confidence = found
        row["lat"] = donor["lat"]
        row["lon"] = donor["lon"]
        row["gps_zdroj"] = FROM_SREALITY
        stats[f"matched ({confidence})"] += 1
        filled += 1

    return filled, stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair", action="store_true",
                        help="re-examine coordinates borrowed earlier, "
                             "instead of filling blank ones")
    parser.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    listings = storage.read_listings(data_dir / "listings.csv")
    if not listings:
        print(f"No listings at {data_dir / 'listings.csv'}.", file=sys.stderr)
        return 1

    session = net.build_session()
    budget = Budget(args.max_seconds)
    walked, errors, scopes = walk_city(session, budget=budget)
    donors = donors_from(walked)
    print(f"{len(walked)} sreality adverts walked, "
          f"{len(donors)} with coordinates")
    for error in errors:
        print(f"  ! {error}")

    now = cas.now()
    now_iso = now.replace(microsecond=0).isoformat()
    state_path = data_dir / "state" / "last_observation.json"
    last_obs = storage.read_last_observation_state(state_path)
    seen_stats, observation_rows, change_rows = observe(
        listings, walked, errors, scopes, last_obs, now_iso)
    print(f"\nstored sreality rows: {seen_stats['updated']} still up, "
          f"{seen_stats['reactivated']} back, "
          f"{seen_stats['missing_marked']} not found "
          f"({seen_stats['removed_confirmed']} now removed)")
    if not seen_stats["scopes_absence_marked"]:
        print("  ! no scope was complete enough to mark absence")
    for error in seen_stats["errors"]:
        print(f"  ! {error}")

    price_by_id = _price_histories(data_dir)

    remembered = price_memory.prune(price_memory.load(data_dir))
    recorded = price_memory.remember(data_dir, donors)
    print(f"remembered today's prices for {recorded} sreality adverts "
          f"({len(remembered)} earlier days kept)")

    if args.repair:
        filled, stats = repair(listings, donors, price_by_id, remembered)
    else:
        filled, stats = lend(listings, donors, price_by_id, remembered)
    print(f"\n{len(listings)} listings")
    for reason, count in stats.most_common():
        print(f"  {count:6d}  {reason}")
    print(f"\n{filled} rows "
          f"{'changed' if args.repair else 'gained'} coordinates.")

    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0

    storage.write_listings(listings, data_dir / "listings.csv")
    storage.append_observations(observation_rows, now,
                                data_dir / "observations")
    storage.append_changes(change_rows, now, data_dir / "changes")
    storage.write_last_observation_state(last_obs, state_path)
    print(f"Wrote {data_dir / 'listings.csv'}, "
          f"{len(observation_rows)} observations, "
          f"{len(change_rows)} attribute changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
