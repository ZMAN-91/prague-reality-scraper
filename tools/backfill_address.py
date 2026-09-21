"""Fill the parsed address fields on rows collected before they existed.

    python -m tools.backfill_address --data-dir store/data [--apply]

listings.csv gained ulice / mestska_cast / obec, and every row written
before that has them blank. They are derived - nothing is lost by recomputing
them - so this simply re-parses `address` for every row.

It writes only those three fields and never touches `address` itself, which
is what the portal said and the thing everything else is derived from. It
also does not touch the house number: that comes from the state address
register, not from the address string, and is tools/backfill_cislo.py's to
fill. See common/address.py on why one field must have one owner.

Re-running is safe and is the point: when the parser learns a shape it did
not know, this puts the improvement through the whole history rather than
only into rows collected afterwards.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import address, storage

FIELDS = ("ulice", "mestska_cast", "obec")


def backfill(listings: dict[str, dict]) -> tuple[int, Counter]:
    """Re-parse every row's address in place. Returns (changed, coverage)."""
    changed = 0
    coverage: Counter = Counter()
    for row in listings.values():
        parsed = address.parse(row.get("address"))
        if any(row.get(field, "") != parsed[field] for field in FIELDS):
            changed += 1
        row.update(parsed)
        for field in FIELDS:
            if parsed[field]:
                coverage[field] += 1
    return changed, coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    path = Path(args.data_dir) / "listings.csv"
    listings = storage.read_listings(path)
    if not listings:
        print(f"No listings at {path}.", file=sys.stderr)
        return 1

    changed, coverage = backfill(listings)
    total = len(listings)
    print(f"{total} listings, {changed} rows would change.")
    for field in FIELDS:
        got = coverage[field]
        print(f"  {field:<14} {got:>6} / {total}  ({100 * got / total:.1f} %)")
    if not args.apply:
        print("Nothing written; pass --apply.")
        return 0

    storage.write_listings(listings, path)
    print(f"Written to {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
