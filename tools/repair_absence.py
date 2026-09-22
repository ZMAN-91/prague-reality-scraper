"""Undo absence that was marked by a walk which never covered the listing.

    python -m tools.repair_absence --data-dir store/data [--apply]

WHAT WENT WRONG

`missing_N` means "this walk covered the population and did not find it".
For one day it meant something else: the hourly sreality walk was narrowed to
Praha 4 and 10 while rent listings from the whole city were already stored,
and it went on reporting its scopes complete. Absence marking believed it,
and 1,201 live adverts in Vinohrady, Smichov, Zizkov and Karlin were called
missing because nothing had looked for them.

scrapers/sreality.py no longer makes that claim. This repairs what it made
while it did.

WHICH ROWS

Only sreality rows, only ones currently marked missing, and only where the
address register puts the coordinate outside Praha 4 and Praha 10 - the two
districts the narrowed walk actually covers. A row inside them was genuinely
looked for, and if it was not found it is genuinely gone; resetting those
would erase real disappearances to tidy up a different mistake.

WHAT IT RESTORES

status back to active, and nothing else. last_seen_at is left alone: it says
2026-09-20 and that is true and is now the only honest signal about these
rows - they were last seen on Sunday and nothing observes them any more. See
the note in the README about what still needs deciding there.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import ruian, storage

#: The districts the narrowed hourly walk covers, as the register names them.
WALKED = {"praha 4", "praha 10"}

SOURCE = "sreality"


def _fold(text) -> str:
    from common import address as address_mod
    return address_mod.strip_diacritics(text or "").lower().strip()


def repair(listings: dict, index) -> tuple:
    """Reset unjustified absence. Returns (restored, stats)."""
    stats: Counter = Counter()
    restored = 0
    for row in listings.values():
        if row.get("source") != SOURCE:
            continue
        if not str(row.get("status") or "").startswith("missing"):
            continue
        stats["sreality rows marked missing"] += 1

        try:
            lat, lon = float(row["lat"]), float(row["lon"])
        except (KeyError, TypeError, ValueError):
            # Without a coordinate there is no way to tell whether the walk
            # covered it, and guessing in either direction is worse than
            # leaving the row exactly as it is.
            stats["no coordinate - left alone"] += 1
            continue

        found = index.match_detail(lat, lon, row.get("ulice"))
        if not found:
            stats["not in the register - left alone"] += 1
            continue
        _fields, point = found
        obvod = _fold(point.get("obvod"))
        if obvod in WALKED:
            stats["inside the walked districts - genuinely absent"] += 1
            continue

        row["status"] = "active"
        restored += 1
        stats[f"restored ({obvod or 'unknown district'})"] += 1
    return restored, stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    index_path = data_dir / "ruian_praha.csv.gz"
    if not index_path.exists():
        print(f"No address index at {index_path}; cannot tell which rows the "
              "walk covered.", file=sys.stderr)
        return 1
    listings = storage.read_listings(data_dir / "listings.csv")
    if not listings:
        print("No listings.", file=sys.stderr)
        return 1

    index = ruian.Index.load(str(index_path))
    restored, stats = repair(listings, index)
    for reason, count in stats.most_common():
        print(f"  {count:6d}  {reason}")
    print(f"\n{restored} rows restored to active.")

    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0
    storage.write_listings(listings, data_dir / "listings.csv")
    print(f"Wrote {data_dir / 'listings.csv'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
