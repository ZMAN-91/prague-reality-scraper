"""Score several pairing strategies against one borough, end to end.

    python -m tools.probe_pairing_strategies --data-dir store/data
        (needs internet; run it on a runner)

Read-only. Collects the borough once, then runs every strategy over the same
rows so the numbers are comparable, and prints one table.

WHY A HARNESS AND NOT ANOTHER ONE-OFF PROBE

Every judgement call in this pairing has now been wrong at least once when
made by reasoning: that the index lacked GPS, that it carried usable_area,
that the flats were not on sreality at all, that loosening the price rule
would find more pairs. Each looked obvious and each was settled the other way
by measurement. So the choice of strategy is made by running them.

WHAT IS VARIED

  donors    sreality alone, or sreality plus the bezrealitky rows already
            stored - those cost no requests at all, being collected hourly
            already, and carry coordinates on every row.

  price     exact, or two-stage: pair on exact price first, then try a looser
            rule on ONLY the rows that found nothing. Measured city-wide,
            loosening the rule globally is worse than exact - 642 pairs and
            24 ambiguous at exact against 633 and 36 at one per cent - because
            a looser rule turns confident pairs into crowded ones. Applying it
            only to the leftovers cannot do that: the pairs already found are
            never re-examined.

Area is fixed at ±1 m2 throughout.

WHAT IS COUNTED

Pairs, ambiguous rows, and how often one advert is lent to several listings -
which is legitimate when two agencies list one flat and a warning sign when
it happens a lot. Then coordinates gained, and house numbers and postcodes
found for them, because a strategy that pairs more and places fewer has not
helped.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import dedup, net, ruian, storage
from common.budget import Budget
from scrapers import idnes, sreality
from tools.lend_gps_from_sreality import AREA_TOLERANCE_M

#: Values a portal writes when it means "not stated". They are not
#: dispositions, and two of them are not a match - bezrealitky writes
#: "undefined" 245 times, and treating that as agreement would pair a flat
#: with any other flat whose size and price happened to line up.
NOT_A_DISPOSITION = {"undefined", "ostatni", "ostatní", "other", "-"}


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def disposition_of(row) -> str:
    value = (row.get("disposition") or "").strip()
    return "" if value.lower() in NOT_A_DISPOSITION else value


def price_exact(a, b):
    return a is not None and b is not None and a == b


def price_within(pct):
    def test(a, b):
        return bool(a and b) and abs(a - b) <= pct / 100.0 * max(a, b)
    return test


def compatible(row, candidate, price_ok) -> bool:
    if row.get("property_type") != candidate.get("property_type"):
        return False
    if row.get("transaction_type") != candidate.get("transaction_type"):
        return False
    disp = disposition_of(row)
    if not disp or disp != disposition_of(candidate):
        return False
    if not price_ok(_f(row.get("price")), _f(candidate.get("price"))):
        return False
    a, b = _f(row.get("area_m2")), _f(candidate.get("area_m2"))
    if a is None or b is None:
        return False
    return abs(a - b) <= AREA_TOLERANCE_M


def run_stages(rows, by_street, stages):
    """Pair `rows` through each price rule in turn.

    A row paired by an earlier stage is never revisited, which is the whole
    point of staging: a looser rule can add answers but cannot take away the
    confident ones an exact rule already found.
    """
    paired = {}
    remaining = list(rows)
    per_stage = []
    crowded = set()
    for price_ok in stages:
        still = []
        found_here = 0
        for row in remaining:
            key = dedup.street_key(row.get("address"))
            hits = [c for c in (by_street.get(key) or [])
                    if compatible(row, c, price_ok)]
            # Several adverts for ONE flat are not an ambiguity: they sit at
            # one coordinate and lend the same answer. Several flats are.
            places = {(h.get("lat"), h.get("lon")) for h in hits}
            if hits and len(places) == 1:
                paired[row["source_id"]] = hits[0]
                found_here += 1
                crowded.discard(row["source_id"])
            else:
                if hits:
                    crowded.add(row["source_id"])
                still.append(row)
        per_stage.append(found_here)
        remaining = still
    # Counted once per row at the end, not once per stage: a row that stays
    # crowded through three stages is one unresolved row, not three.
    return paired, len(crowded), remaining, per_stage


def as_row(item, source):
    return {
        "source": source, "source_id": item.source_id,
        "property_type": item.property_type,
        "transaction_type": item.transaction_type,
        "disposition": item.disposition or "",
        "area_m2": item.area_m2, "price": item.price,
        "lat": item.lat, "lon": item.lon,
        "address": item.address or "",
    }


def stored_donors(data_dir: Path, source: str):
    """Rows this project already collects, reused as donors for free."""
    listings = storage.read_listings(data_dir / "listings.csv")
    state = storage.read_last_observation_state(
        data_dir / "state" / "last_observation.json")
    prices = {i: s.get("price") for i, s in (state or {}).items()}
    out = []
    for row in listings.values():
        if row.get("source") != source:
            continue
        lat, lon = _f(row.get("lat")), _f(row.get("lon"))
        if lat is None or lon is None:
            continue
        donor = dict(row)
        donor["lat"], donor["lon"] = lat, lon
        donor["price"] = prices.get(row["internal_id"])
        out.append(donor)
    return out


def bucket(rows):
    by_street = {}
    for row in rows:
        key = dedup.street_key(row.get("address"))
        if key:
            by_street.setdefault(key, []).append(row)
    return by_street


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--max-seconds", type=int, default=2400)
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    session = net.build_session()
    budget = Budget(args.max_seconds)

    print("== collecting Praha 10")
    s_items, _p, s_err, _s = sreality.fetch_all(
        session, budget, districts=(sreality.DISTRICT_PRAHA_10,))
    sreality_rows = [as_row(i, "sreality") for i in s_items]
    i_items, _p, i_err, _c, _pg, _cur = idnes.fetch_all(
        session, budget, branches=("praha-10",), max_watched=100_000)
    idnes_rows = [as_row(i, "idnes") for i in i_items]
    print(f"  sreality {len(sreality_rows)}, iDNES {len(idnes_rows)}")
    for e in (s_err + i_err)[:4]:
        print(f"    ! {e}")
    if not sreality_rows or not idnes_rows:
        print("one side empty; nothing can be concluded")
        return 1

    bez = stored_donors(data_dir, "bezrealitky")
    print(f"  bezrealitky (already stored, no requests) {len(bez)}")

    index_path = data_dir / "ruian_praha.csv.gz"
    index = ruian.Index.load(str(index_path)) if index_path.exists() else None

    donor_sets = [
        ("sreality", sreality_rows),
        ("sreality+bezrealitky", sreality_rows + bez),
    ]
    stage_sets = [
        ("exact", [price_exact]),
        ("exact -> 1%", [price_exact, price_within(1)]),
        ("exact -> 5%", [price_exact, price_within(5)]),
        ("exact -> 10%", [price_exact, price_within(10)]),
    ]

    print(f"\n== strategies, area fixed at ±{AREA_TOLERANCE_M} m2, "
          f"over {len(idnes_rows)} iDNES rows")
    header = (f"  {'donors':22s} {'price':14s} {'paired':>7s} {'ambig':>6s} "
              f"{'reused':>7s} {'gps':>6s} {'number':>7s} {'psc':>6s}")
    print(header)
    for donor_name, donors in donor_sets:
        by_street = bucket(donors)
        for stage_name, stages in stage_sets:
            paired, ambiguous, _rest, per_stage = run_stages(
                idnes_rows, by_street, stages)
            reuse = Counter(id(t) for t in paired.values())
            reused = sum(1 for n in reuse.values() if n > 1)

            gps = sum(1 for t in paired.values() if t.get("lat") and t.get("lon"))
            number = psc = 0
            if index:
                for source_id, twin in paired.items():
                    if not (twin.get("lat") and twin.get("lon")):
                        continue
                    row = next(r for r in idnes_rows
                               if r["source_id"] == source_id)
                    found = index.match_detail(
                        twin["lat"], twin["lon"],
                        dedup.street_key(row.get("address")))
                    if not found:
                        continue
                    fields, point = found
                    if fields["cislo_popisne"]:
                        number += 1
                    if point.get("psc"):
                        psc += 1
            print(f"  {donor_name:22s} {stage_name:14s} {len(paired):7d} "
                  f"{ambiguous:6d} {reused:7d} {gps:6d} {number:7d} {psc:6d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
