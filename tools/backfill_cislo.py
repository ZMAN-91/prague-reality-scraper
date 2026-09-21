"""Give every listing that can have a house number one, and report the rest.

    python -m tools.backfill_cislo --data-dir store/data [--apply]

Re-runnable, and meant to be re-run: a better index, a listing that gains
coordinates from its cluster, or a fix to the street parser all put more rows
within reach, and this puts the improvement through the whole history rather
than only into rows collected afterwards.

WHAT IT PRINTS IS THE POINT

Whether this feature is worth anything is a question about the portals' pins,
and nobody knows the answer until it is measured. If the median match sits at
8 m the portals geocode to the building and the numbers are real; at 80 m
they geocode to the street and the numbers are a coin toss between
neighbours. So the run prints the distribution of match distances and of
candidate counts, and prints why each unmatched row was unmatched.

That report is the deliverable as much as the column is. It is what says how
far to trust the column.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import ruian, storage


def backfill(listings: dict, index: ruian.Index) -> tuple:
    """Fill the match fields in place. Returns (changed, stats)."""
    changed = 0
    stats: Counter = Counter()
    distances = []
    crowds: Counter = Counter()

    for row in listings.values():
        lat = _number(row.get("lat"))
        lon = _number(row.get("lon"))
        ulice = (row.get("ulice") or "").strip()

        if lat is None or lon is None:
            stats["no coordinates"] += 1
            match = None
        elif not ulice:
            stats["no street"] += 1
            match = None
        else:
            match = index.match(lat, lon, ulice)
            if match is None:
                # Which of the two it is matters: a street the register does
                # not know is a parser or an abbreviation problem and can be
                # fixed, while a point too far away is the portal's pin and
                # cannot.
                if index.by_street.get(_fold(ulice)):
                    stats["nearest point too far"] += 1
                else:
                    stats["street not in the register"] += 1

        new = match or ruian.blank_match()
        if any(row.get(field, "") != new[field]
               for field in ruian.MATCH_FIELDS):
            changed += 1
        row.update(new)

        if match:
            stats["matched"] += 1
            distances.append(float(match["cislo_vzdalenost_m"]))
            crowds[int(match["cislo_kandidatu"])] += 1

    return changed, {"reasons": stats, "distances": distances,
                     "crowds": crowds}


def _fold(ulice: str) -> str:
    from common import address
    return address.strip_diacritics(ulice).lower().strip()


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percentile(values, share: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(share * len(ordered)))
    return ordered[index]


def report(total: int, stats: dict) -> None:
    reasons = stats["reasons"]
    distances = stats["distances"]
    crowds = stats["crowds"]

    print(f"{total} listings")
    for reason, count in reasons.most_common():
        print(f"  {count:6d}  {reason}  ({100.0 * count / total:.1f}%)")

    if not distances:
        print("\nNothing matched, so there is no distribution to report.")
        return

    print(f"\nHow far the portal's pin was from the address point, "
          f"over {len(distances)} matches:")
    for share in (0.10, 0.25, 0.50, 0.75, 0.90, 0.99):
        print(f"  p{int(share * 100):02d}  {percentile(distances, share):7.1f} m")
    print(f"  max  {max(distances):7.1f} m")

    under = sum(1 for d in distances if d <= 25.0)
    print(f"\n  {under} of {len(distances)} matches are within 25 m "
          f"({100.0 * under / len(distances):.1f}%) - about one building.")

    print("\nHow many different houses were about equally close:")
    for count in sorted(crowds):
        share = 100.0 * crowds[count] / len(distances)
        print(f"  {count:3d} candidate(s)  {crowds[count]:6d}  ({share:.1f}%)")

    alone = crowds.get(1, 0)
    print(f"\n  {alone} matches ({100.0 * alone / len(distances):.1f}%) name "
          "one house with nothing else nearby.")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--index", default=None,
                        help="the gzipped index; defaults to "
                             "<data-dir>/ruian_praha.csv.gz")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    index_path = Path(args.index) if args.index \
        else data_dir / "ruian_praha.csv.gz"
    if not index_path.exists():
        print(f"No address index at {index_path}. Build it with "
              "tools.build_ruian_index first.", file=sys.stderr)
        return 1

    listings = storage.read_listings(data_dir / "listings.csv")
    if not listings:
        print(f"No listings at {data_dir / 'listings.csv'}.", file=sys.stderr)
        return 1

    index = ruian.Index.load(str(index_path))
    print(f"{len(index)} address points on {index.streets} streets\n")

    changed, stats = backfill(listings, index)
    report(len(listings), stats)
    print(f"\n{changed} rows would change.")

    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0

    storage.write_listings(listings, data_dir / "listings.csv")
    print(f"Wrote {data_dir / 'listings.csv'}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
