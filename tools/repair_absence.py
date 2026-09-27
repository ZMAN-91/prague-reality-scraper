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

AND NOT WHAT A CITY WALK OBSERVED

Since 23 September sreality is walked city-wide every night, and that walk
marks absence across all of Prague - legitimately, because it covers all of
Prague. This tool predates that and did not know it: on 27 September a dry
run offered to "restore" 333 listings the city walk had looked for and not
found, and the checklists said a non-zero dry run meant --apply.

So a row is left alone when a sreality city walk that completed the row's
category ran after the row was last seen. The run log says which walks
completed which categories; that is what check_sweep_freshness reads too.
The rows this tool was written for - marked by the narrowed hourly walk,
with no city walk after them - still qualify, and only those do.

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


#: What the nightly walk writes into the run log. See
#: tools/lend_gps_from_sreality.py.
CITY_WALK_KIND = "sreality city walk"


def city_walks(logs_dir) -> list:
    """[(started_at, {"byt/prodej", ...})] for every sreality city walk in
    the run log, with the categories it walked to completion."""
    import json
    walks = []
    if logs_dir is None:
        return walks
    for path in sorted(Path(logs_dir).glob("*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("kind") != CITY_WALK_KIND:
                    continue
                done = (entry.get("sources") or {}).get(SOURCE, {}) \
                    .get("scopes_absence_marked") or []
                if done:
                    walks.append((str(entry.get("started_at") or ""), set(done)))
    return walks


def _observed_since(row: dict, walks: list) -> bool:
    """Did a city walk covering this row's category run after it was last
    seen? Then its absence was looked for, and is real."""
    scope = f"{row.get('property_type')}/{row.get('transaction_type')}"
    seen = str(row.get("last_seen_at") or "")[:10]
    return any(started[:10] > seen and scope in done for started, done in walks)


def repair(listings: dict, index, walks: list = ()) -> tuple:
    """Reset unjustified absence. Returns (restored, stats)."""
    stats: Counter = Counter()
    restored = 0
    for row in listings.values():
        if row.get("source") != SOURCE:
            continue
        if not str(row.get("status") or "").startswith("missing"):
            continue
        stats["sreality rows marked missing"] += 1

        if _observed_since(row, walks):
            stats["looked for by a city walk since last seen - genuinely absent"] += 1
            continue

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
    parser.add_argument("--logs-dir", default=None,
                        help="run log, for which city walks have covered "
                             "which rows (default: logs/ beside data/)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)
    logs_dir = Path(args.logs_dir) if args.logs_dir else data_dir.parent / "logs"

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
    walks = city_walks(logs_dir)
    print(f"{len(walks)} sreality city walks in the run log")
    restored, stats = repair(listings, index, walks)
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
