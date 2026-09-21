"""Pair Praha 10 across sreality and iDNES, end to end, and count everything.

    python -m tools.probe_praha10_pairing   (needs internet; run it on a runner)

Read-only. Collects one borough from both portals, pairs them, lends
coordinates across each pair, looks the pairs up in the address register, and
prints a single table of what happened at every stage.

WHY ONE BOROUGH

Because the answer has to be checkable. City-wide numbers hid a missing field
for a whole day: every sreality advert had area None, ten different pairing
rules returned the same zero, and the zero read as "these flats are not on
sreality" rather than "this column is empty". One borough is small enough to
print examples beside the totals, so a wrong number looks wrong.

WHAT IT REPORTS

  - how many adverts each portal has in Praha 10
  - how many pair, and how many are left unpaired on each side
  - for the unpaired iDNES rows, which requirement the closest candidate
    fell at, so a rule that is too strict is distinguishable from a flat
    that genuinely has no twin
  - how many iDNES rows gain coordinates from their pair
  - of those, how many get a house number and a postcode from RUIAN

Several price rules are scored side by side. The area rule is fixed at the
±1 m2 asked for, and the count it costs is reported rather than assumed.
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

PRICE_RULES = [
    ("exact", lambda a, b: a is not None and b is not None and a == b),
    ("within 1%", lambda a, b: bool(a and b) and abs(a - b) <= 0.01 * max(a, b)),
    ("within 5%", lambda a, b: bool(a and b) and abs(a - b) <= 0.05 * max(a, b)),
    ("both known, any value", lambda a, b: a is not None and b is not None),
    ("not tested", lambda a, b: True),
]


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_row(item, source):
    return {
        "source": source,
        "source_id": item.source_id,
        "property_type": item.property_type,
        "transaction_type": item.transaction_type,
        "disposition": item.disposition or "",
        "area_m2": item.area_m2,
        "price": item.price,
        "lat": item.lat,
        "lon": item.lon,
        "address": item.address or "",
        "url": item.url or "",
    }


def pairs_under(idnes_rows, by_street, price_ok):
    """(paired, unpaired, ambiguous) under one price rule."""
    paired = {}
    unpaired = []
    ambiguous = 0
    for row in idnes_rows:
        key = dedup.street_key(row.get("address"))
        hits = []
        for candidate in (by_street.get(key) or []):
            if row["property_type"] != candidate["property_type"]:
                continue
            if row["transaction_type"] != candidate["transaction_type"]:
                continue
            disp = (row.get("disposition") or "").strip()
            if not disp or disp != (candidate.get("disposition") or "").strip():
                continue
            if not price_ok(_f(row.get("price")), _f(candidate.get("price"))):
                continue
            a, b = _f(row.get("area_m2")), _f(candidate.get("area_m2"))
            if a is None or b is None or abs(a - b) > AREA_TOLERANCE_M:
                continue
            hits.append(candidate)
        distinct = {(h["source_id"]) for h in hits}
        if not hits:
            unpaired.append(row)
        elif len(distinct) == 1:
            paired[row["source_id"]] = hits[0]
        else:
            ambiguous += 1
            unpaired.append(row)
    return paired, unpaired, ambiguous


def why_not(row, by_street):
    key = dedup.street_key(row.get("address"))
    candidates = by_street.get(key) or []
    if not key:
        return "listing has no usable street"
    if not candidates:
        return "no sreality advert on that street"
    best = -1
    order = ["property type", "transaction type", "disposition",
             "area beyond 1 m2", "price"]
    for candidate in candidates:
        if row["property_type"] != candidate["property_type"]:
            reached = 0
        elif row["transaction_type"] != candidate["transaction_type"]:
            reached = 1
        elif not ((row.get("disposition") or "").strip()
                  and (row.get("disposition") or "").strip()
                  == (candidate.get("disposition") or "").strip()):
            reached = 2
        else:
            a, b = _f(row.get("area_m2")), _f(candidate.get("area_m2"))
            if a is None or b is None or abs(a - b) > AREA_TOLERANCE_M:
                reached = 3
            else:
                reached = 4
        best = max(best, reached)
    return order[best]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--max-seconds", type=int, default=2400)
    args = parser.parse_args(argv)

    session = net.build_session()
    budget = Budget(args.max_seconds)

    print("== collecting Praha 10")
    s_items, _p, s_err, _s = sreality.fetch_all(
        session, budget, districts=(sreality.DISTRICT_PRAHA_10,))
    sreality_rows = [as_row(i, "sreality") for i in s_items]
    print(f"  sreality  {len(sreality_rows)} adverts")
    for e in s_err[:5]:
        print(f"    ! {e}")

    # max_watched caps how many listings get a full detail read. Its default
    # is an hourly run's budget; here the whole borough is the sample, so a
    # cap would silently make the iDNES side a subset and every "unpaired"
    # count wrong in the same direction.
    i_items, _p, i_err, _c, _pg, _cur = idnes.fetch_all(
        session, budget, branches=("praha-10",), max_watched=100_000)
    idnes_rows = [as_row(i, "idnes") for i in i_items]
    print(f"  iDNES     {len(idnes_rows)} adverts")
    for e in i_err[:5]:
        print(f"    ! {e}")

    if not sreality_rows or not idnes_rows:
        print("\none side came back empty; nothing can be concluded")
        return 1

    for label, rows in (("sreality", sreality_rows), ("iDNES", idnes_rows)):
        have_area = sum(1 for r in rows if _f(r.get("area_m2")))
        have_price = sum(1 for r in rows if _f(r.get("price")))
        have_gps = sum(1 for r in rows if r.get("lat") and r.get("lon"))
        have_disp = sum(1 for r in rows if (r.get("disposition") or "").strip())
        print(f"\n  {label}: area {have_area}/{len(rows)}, "
              f"price {have_price}/{len(rows)}, gps {have_gps}/{len(rows)}, "
              f"disposition {have_disp}/{len(rows)}")

    by_street = {}
    for row in sreality_rows:
        key = dedup.street_key(row.get("address"))
        if key:
            by_street.setdefault(key, []).append(row)

    print(f"\n== pairing, area fixed at ±{AREA_TOLERANCE_M} m2")
    print(f"  {'price rule':24s} {'paired':>8s} {'ambiguous':>10s} "
          f"{'unpaired':>9s}")
    results = {}
    for name, price_ok in PRICE_RULES:
        paired, unpaired, ambiguous = pairs_under(
            idnes_rows, by_street, price_ok)
        results[name] = (paired, unpaired, ambiguous)
        print(f"  {name:24s} {len(paired):8d} {ambiguous:10d} "
              f"{len(unpaired):9d}")

    chosen = "within 1%"
    paired, unpaired, _amb = results[chosen]
    print(f"\n== the rest, under the '{chosen}' rule")
    print(f"  {len(paired)} of {len(idnes_rows)} iDNES adverts paired "
          f"({100.0 * len(paired) / len(idnes_rows):.1f}%)")
    print(f"  {len(sreality_rows) - len(paired)} sreality adverts unpaired")

    reasons = Counter(why_not(r, by_street) for r in unpaired)
    print("\n  why the unpaired iDNES rows did not pair:")
    for reason, count in reasons.most_common():
        print(f"    {count:6d}  {reason}")

    gained = 0
    for source_id, twin in paired.items():
        if twin.get("lat") and twin.get("lon"):
            gained += 1
    print(f"\n== coordinates\n  {gained} of {len(paired)} pairs can lend GPS")

    index_path = Path(args.data_dir) / "ruian_praha.csv.gz"
    if not index_path.exists():
        print(f"\nNo address index at {index_path}; stopping before numbers.")
        return 0
    index = ruian.Index.load(str(index_path))
    print(f"\n== house numbers, over the {gained} rows that gained GPS")

    got_number = got_psc = 0
    distances = []
    for source_id, twin in paired.items():
        if not (twin.get("lat") and twin.get("lon")):
            continue
        row = next(r for r in idnes_rows if r["source_id"] == source_id)
        found = index.match_detail(twin["lat"], twin["lon"],
                                  dedup.street_key(row.get("address")))
        if not found:
            continue
        fields, point = found
        if fields["cislo_popisne"]:
            got_number += 1
            distances.append(float(fields["cislo_vzdalenost_m"]))
        if point.get("psc"):
            got_psc += 1
    if gained:
        print(f"  house number  {got_number}/{gained} "
              f"({100.0 * got_number / gained:.1f}%)")
        print(f"  postcode      {got_psc}/{gained} "
              f"({100.0 * got_psc / gained:.1f}%)")
    if distances:
        distances.sort()
        print(f"  median pin-to-point distance "
              f"{distances[len(distances) // 2]:.1f} m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
