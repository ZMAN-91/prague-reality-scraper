"""Are the missing flats on sreality at all, and under which rule do we find them?

    python -m tools.probe_gps_donor_rules --data-dir store/data
        (needs internet; run it on a runner)

Read-only. Writes nothing, decides nothing, changes no data.

WHY THIS EXISTS

A first attempt at borrowing coordinates from sreality matched zero of 5,188
listings, and the reasoning that followed was wrong twice over:

  - It was validated on iDNES rows that already had a cluster twin. Those
    rows were selected by the same price-and-area test being validated, so
    the sample could not have failed. Re-deriving 273 of 400 proved only
    that the rule reproduces itself.

  - The failure report named the criterion where the FURTHEST-getting
    candidate stopped. That is not the true twin's failure point. "Fell at
    area" is consistent with the real twin having failed earlier, at the
    exact-price test, while an unrelated flat on the street got further.

So this asks the data instead. It prints two things and draws no conclusion:

  1. A COUNT PER RULE. The same candidates scored under several definitions
     of "same flat", so the cost of each requirement is visible as a number
     rather than argued about. Ambiguous cases are counted separately from
     matches - a rule that finds more pairs by making them indistinguishable
     has not found anything.

  2. CONCRETE ROWS. For a sample of unmatched listings, the listing and every
     sreality advert on its street, with disposition, area and price side by
     side. Whether a twin is sitting there is then a matter of looking, not
     of inference.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import dedup, net, storage
from common.budget import Budget
from tools.lend_gps_from_sreality import collect_donors


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: The rules compared. Each is (name, price test, area test), where a test
#: takes the two values and says whether they are close enough. Deliberately
#: spelled out rather than parameterised: the point is to read them.
def _price_exact(a, b):
    return a is not None and b is not None and a == b


def _price_within(pct):
    def test(a, b):
        if a is None or b is None:
            return False
        return abs(a - b) <= pct / 100.0 * max(a, b)
    return test


def _price_known_only(a, b):
    return a is not None and b is not None


def _price_any(a, b):
    return True


def _area_abs(metres):
    def test(a, b):
        if a is None or b is None:
            return False
        return abs(a - b) <= metres
    return test


def _area_rel(pct):
    def test(a, b):
        if a is None or b is None:
            return False
        return abs(a - b) <= pct / 100.0 * max(a, b)
    return test


RULES = [
    ("exact price + area 15%      (what shipped)", _price_exact, _area_rel(15)),
    ("exact price + area 2 m2",                    _price_exact, _area_abs(2)),
    ("price 1%    + area 2 m2",                    _price_within(1), _area_abs(2)),
    ("price 2%    + area 2 m2",                    _price_within(2), _area_abs(2)),
    ("price 5%    + area 2 m2",                    _price_within(5), _area_abs(2)),
    ("price 10%   + area 2 m2",                    _price_within(10), _area_abs(2)),
    ("both prices known, any value + area 2 m2",   _price_known_only, _area_abs(2)),
    ("no price test at all + area 2 m2",           _price_any, _area_abs(2)),
    ("price 5%    + area 1 m2",                    _price_within(5), _area_abs(1)),
    ("price 5%    + area 5%",                      _price_within(5), _area_rel(5)),
]


def candidates_for(row, by_street):
    key = dedup.street_key(row.get("address"))
    if not key:
        return None
    return by_street.get(key) or []


def score(row, candidates, price_ok, area_ok):
    """(matches, ties) for one row under one rule."""
    hits = []
    for candidate in candidates:
        if row.get("property_type") != candidate.get("property_type"):
            continue
        if row.get("transaction_type") != candidate.get("transaction_type"):
            continue
        disp_a = (row.get("disposition") or "").strip()
        disp_b = (candidate.get("disposition") or "").strip()
        if not disp_a or disp_a != disp_b:
            continue
        if not price_ok(_f(row.get("price")), _f(candidate.get("price"))):
            continue
        if not area_ok(_f(row.get("area_m2")), _f(candidate.get("area_m2"))):
            continue
        hits.append(candidate)
    return hits


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--examples", type=int, default=12)
    parser.add_argument("--max-seconds", type=int, default=1200)
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    listings = storage.read_listings(data_dir / "listings.csv")
    state = storage.read_last_observation_state(
        data_dir / "state" / "last_observation.json")
    prices = {i: s.get("price") for i, s in (state or {}).items()}

    session = net.build_session()
    donors, errors = collect_donors(session, budget=Budget(args.max_seconds))
    print(f"{len(donors)} sreality adverts with coordinates")
    for error in errors:
        print(f"  ! {error}")

    by_street = {}
    for donor in donors:
        key = dedup.street_key(donor.get("address"))
        if key:
            by_street.setdefault(key, []).append(donor)
    print(f"{len(by_street)} distinct streets on the sreality side\n")

    wanted = []
    for row in listings.values():
        if str(row.get("lat") or "").strip() and str(row.get("lon") or "").strip():
            continue
        probe = dict(row)
        probe["price"] = prices.get(row["internal_id"])
        wanted.append(probe)
    print(f"{len(wanted)} listings without coordinates\n")

    # How far each row gets before any rule is applied.
    reach = Counter()
    for row in wanted:
        cands = candidates_for(row, by_street)
        if cands is None:
            reach["no usable street"] += 1
        elif not cands:
            reach["street has no sreality advert"] += 1
        else:
            reach["street has sreality adverts"] += 1
    print("== reachability, before any sameness rule")
    for key, count in reach.most_common():
        print(f"  {count:6d}  {key}")

    print(f"\n== one row per rule, over the {reach['street has sreality adverts']} "
          "rows whose street has adverts")
    print(f"  {'rule':46s} {'one match':>10s} {'ambiguous':>10s} {'none':>8s}")
    for name, price_ok, area_ok in RULES:
        one = ambiguous = none = 0
        for row in wanted:
            cands = candidates_for(row, by_street)
            if not cands:
                continue
            hits = score(row, cands, price_ok, area_ok)
            # Several adverts for ONE flat is not ambiguity; several
            # different flats is. Judged by area+price, the only things that
            # separate them here.
            distinct = {(_f(h.get("area_m2")), _f(h.get("price"))) for h in hits}
            if len(hits) == 0:
                none += 1
            elif len(distinct) == 1:
                one += 1
            else:
                ambiguous += 1
        print(f"  {name:46s} {one:10d} {ambiguous:10d} {none:8d}")

    print(f"\n== {args.examples} unmatched rows, with every sreality advert "
          "on their street")
    print("   (nothing is inferred here - look and judge)")
    shown = 0
    for row in wanted:
        cands = candidates_for(row, by_street)
        if not cands:
            continue
        same_kind = [c for c in cands
                     if c.get("transaction_type") == row.get("transaction_type")
                     and c.get("property_type") == row.get("property_type")]
        if not same_kind:
            continue
        print(f"\n  iDNES  {row.get('address','')[:46]:46s} "
              f"{(row.get('disposition') or ''):8s} "
              f"{str(row.get('area_m2') or ''):>7s} m2  "
              f"{str(row.get('price') or '-'):>12s}")
        for c in same_kind[:6]:
            print(f"    sreality  {(c.get('address') or '')[:44]:44s} "
                  f"{(c.get('disposition') or ''):8s} "
                  f"{str(c.get('area_m2') or ''):>7s} m2  "
                  f"{str(c.get('price') or '-'):>12s}")
        shown += 1
        if shown >= args.examples:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
