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

THE CROSS-CHECK THAT COSTS NOTHING

The register knows which district each of its points is in. The matching
never looks at that field, and the portals state a district of their own - so
comparing the two asks an independent authority whether the building this
picked is in the right part of town.

Measured on the first real run: 98.4% agreement over 3,961 rows, and the
disagreements are nearly all streets on a cadastral boundary, where the
register is the more correct of the two - Narodni IS the line between Stare
Mesto and Nove Mesto.

It is reported every month because it is the check that would catch the
failure nothing else would. If the coordinate conversion ever broke - a PROJ
upgrade changing which datum shift it prefers, a sign lost - every match
would still be produced, still carry a plausible distance, and still look
entirely normal. District agreement would collapse in the same instant.
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
    districts: Counter = Counter()

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
            match = index.match_detail(lat, lon, ulice)
            if match is None:
                # Which of the two it is matters: a street the register does
                # not know is a parser or an abbreviation problem and can be
                # fixed, while a point too far away is the portal's pin and
                # cannot.
                if index.by_street.get(_fold(ulice)):
                    stats["nearest point too far"] += 1
                else:
                    stats["street not in the register"] += 1

        if match:
            fields, register_row = match
            _cross_check(row, register_row, districts)
            filled = _fill_district(row, register_row, stats)
        else:
            fields = None
            filled = _fill_district_from_street(row, index, stats)

        new = fields or ruian.blank_match()
        # A district filled in is a changed row too, and counted once with
        # the match fields rather than twice. Without it the summary read
        # "0 rows would change" on the run that gave 1,858 listings the
        # district they had never had - and an --apply that reports nothing
        # changed is one nobody runs twice.
        if filled or any(row.get(field, "") != new[field]
                         for field in ruian.MATCH_FIELDS):
            changed += 1
        row.update(new)

        if match:
            stats["matched"] += 1
            distances.append(float(fields["cislo_vzdalenost_m"]))
            crowds[int(fields["cislo_kandidatu"])] += 1

    return changed, {"reasons": stats, "distances": distances,
                     "crowds": crowds, "districts": districts}


def _fill_district(row: dict, register_row: dict, stats: Counter) -> bool:
    """Give a district to a listing whose portal named none.

    2,061 of 13,408 rows carry no mestska_cast, and 1,866 of those are every
    single bezrealitky listing - that portal does not publish one at all. The
    register does, for the very building this row was just matched to, so the
    answer was already in hand and was being thrown away.

    Only ever fills a blank. A district the portal stated is evidence and
    stays, even when the register disagrees: that disagreement is the whole
    point of the cross-check above, and overwriting it would delete the
    finding rather than record it.
    """
    if (row.get("mestska_cast") or "").strip():
        return False
    district = _fold_keep_case(register_row.get("cast_obce") or "")
    if not district:
        return False
    row["mestska_cast"] = district
    row["mestska_cast_zdroj"] = ruian.SOURCE_NAME
    stats["district from the register"] += 1
    return True


def _fill_district_from_street(row: dict, index, stats: Counter) -> bool:
    """The fallback for a row with no register match: the street itself.

    Only when the street runs through exactly one district in the whole
    register. Plenty do not - Evropska crosses four - and for those this
    says nothing rather than guessing, because a guessed district would go
    into the same column as a stated one and there would be no way back.

    And only for a listing in Prague. The register loaded here is Prague's,
    so a street name it recognises means "Prague has a street by that name",
    not "this flat is on it". Measured before that condition was added: of
    42 districts this supplied, 36 were wrong - a flat on Zitna in Hostivice
    was placed in Nove Mesto, one on Riegrova in Cernosice in Klanovice, one
    on Kralupska in Brandys nad Labem in Ruzyne. bezrealitky sweeps Prague
    AND the ring of towns around it, and street names repeat out there.
    """
    if (row.get("mestska_cast") or "").strip():
        return False
    if (row.get("obec") or "").strip() != "Praha":
        stats["outside Prague, so the register cannot place it"] += 1
        return False
    ulice = (row.get("ulice") or "").strip()
    if not ulice:
        return False
    district = _sole_district(index, ulice)
    if not district:
        return False
    row["mestska_cast"] = district
    row["mestska_cast_zdroj"] = "ulice"
    stats["district from the street"] += 1
    return True


def _sole_district(index, ulice: str) -> str:
    """The one district a street runs through, or "" when it runs through
    more than one (or the register has never heard of it)."""
    points = index.by_street.get(_fold(ulice)) or []
    districts = {(point.get("cast_obce") or "").strip() for point in points}
    districts.discard("")
    if len(districts) != 1:
        return ""
    return _fold_keep_case(districts.pop())


def _fold_keep_case(value: str) -> str:
    """Diacritics off, capitalisation kept: the register writes "Hradcany"
    as "Hrad\u010dany" and the portals write "Vinohrady", so stripping the
    accents is all it takes for the two to be the same column."""
    from common import address
    return address.strip_diacritics(value).strip()


def _cross_check(row: dict, register_row: dict, districts: Counter) -> None:
    """Does the register put this building in the district the portal named?

    The portal says either a cadastral district ("Zabehlice") or a borough
    ("Praha 10"), and the register carries both, so either counts as
    agreement. Folded on both sides - the register keeps its diacritics, and
    comparing "vysocany" against "Vysocany" without folding reports every
    single row as a disagreement. It did, the first time this was measured by
    hand.
    """
    # Only a district the PORTAL named. A district this tool filled in from
    # the register on an earlier run would be compared against the register
    # that produced it, agree by construction, and push the figure to 100%
    # - retiring the one check that would notice a broken coordinate
    # conversion, by feeding it its own output.
    if (row.get("mestska_cast_zdroj") or "") not in ("", "portal"):
        districts["not the portal's district"] += 1
        return
    stated = _fold(row.get("mestska_cast") or "")
    if not stated:
        districts["portal named no district"] += 1
        return
    known = {_fold(register_row.get("cast_obce") or ""),
             _fold(register_row.get("obvod") or ""),
             _fold(register_row.get("mestska_cast") or "")}
    districts["agree" if stated in known else "disagree"] += 1


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

    districts = stats.get("districts") or Counter()
    checked = districts["agree"] + districts["disagree"]
    if checked:
        share = 100.0 * districts["agree"] / checked
        print(f"\nCross-check - the register's own district for the matched "
              f"building\nagainst the district the portal stated, over "
              f"{checked} rows that stated one:")
        print(f"  agree     {districts['agree']:6d}  ({share:.1f}%)")
        print(f"  disagree  {districts['disagree']:6d}  "
              f"({100.0 - share:.1f}%)")
        print(f"  (portal named no district: "
              f"{districts['portal named no district']})")
        print("\n  Nothing in the matching uses this field, so it is an "
              "independent\n  check. A broken coordinate conversion would "
              "still produce matches,\n  still with plausible distances - "
              "and would show up here at once.")


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
