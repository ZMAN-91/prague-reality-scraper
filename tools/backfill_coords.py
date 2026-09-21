"""Lend coordinates within clusters on rows collected before run.py did it.

    python -m tools.backfill_coords --data-dir store/data [--apply]

Same operation the hourly run now performs after clustering, applied to the
history. Re-runnable: each improvement to the matcher puts more adverts in a
cluster, and every one of those may carry coordinates to a row that has none.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from common import coords, storage


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

    def with_coords() -> int:
        return sum(1 for r in listings.values()
                   if str(r.get("lat") or "").strip() and str(r.get("lon") or "").strip())

    before = with_coords()
    filled = coords.lend_within_clusters(listings)
    total = len(listings)
    print(f"{total} listings: {before} had coordinates ({100 * before / total:.1f} %), "
          f"{filled} lent from a cluster sibling, "
          f"now {with_coords()} ({100 * with_coords() / total:.1f} %).")
    if not args.apply:
        print("Nothing written; pass --apply.")
        return 0
    storage.write_listings(listings, path)
    print(f"Written to {path}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
