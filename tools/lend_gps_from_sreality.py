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

THE DONORS ARE NOT COLLECTED

They are read, used, and dropped. Storing them would change what the dataset
means - sreality is collected for the watched area, deliberately - and would
hand absence-marking a population it never walked. Nothing here writes a new
listing; the only thing that changes is lat/lon/gps_zdroj on rows that had
none.

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

from common import cas, dedup, net, storage
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


def collect_donors(session, budget=None, districts=None) -> list:
    """sreality's Prague index, as rows shaped like listings.csv.

    Only rows that carry a coordinate: a donor without one has nothing to
    give, and keeping it would only slow the matching down.
    """
    listings, _pages, errors, _scopes = sreality.fetch_all(
        session,
        budget=budget,
        districts=districts or (sreality.DISTRICT_PRAHA,),
    )

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
    return donors, errors


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


def lend(listings: dict, donors: list, prices: dict = None) -> tuple:
    """Fill blank coordinates from a matching donor. Returns (filled, stats)."""
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
        if row["internal_id"] in prices:
            probe["price"] = prices[row["internal_id"]]

        found = dedup.best_match(probe, candidates,
                                 area_abs_m=AREA_TOLERANCE_M)
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
    parser.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    listings = storage.read_listings(data_dir / "listings.csv")
    if not listings:
        print(f"No listings at {data_dir / 'listings.csv'}.", file=sys.stderr)
        return 1

    session = net.build_session()
    budget = Budget(args.max_seconds)
    donors, errors = collect_donors(session, budget=budget)
    print(f"{len(donors)} sreality adverts with coordinates")
    for error in errors:
        print(f"  ! {error}")

    prices = storage.read_last_observation_state(data_dir / "state" /
                                                 "last_observation.json")
    price_by_id = {i: s.get("price") for i, s in (prices or {}).items()}

    filled, stats = lend(listings, donors, price_by_id)
    print(f"\n{len(listings)} listings")
    for reason, count in stats.most_common():
        print(f"  {count:6d}  {reason}")
    print(f"\n{filled} rows gained coordinates.")

    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0

    storage.write_listings(listings, data_dir / "listings.csv")
    print(f"Wrote {data_dir / 'listings.csv'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
