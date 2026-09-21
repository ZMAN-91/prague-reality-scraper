"""Turn the state address register's Prague export into a local index.

    python -m tools.build_ruian_index --out store/data/ruian_praha.csv.gz

Runs monthly, on a runner, because cuzk.cz publishes one file per month and
because this is the only part of the project that needs pyproj. It downloads
the export, converts 134,627 points out of S-JTSK, and writes the columns the
matcher needs and nothing else. Everything downstream - the hourly scrape
included - reads the result with the standard library.

WHAT THE REGISTER CALLS THINGS

Czech addresses carry two numbers and the register keeps them apart:

    Cislo domovni   the number on the red plate, unique within the cadastral
                    district. Filled for 100% of rows. Whether it is a
                    cislo popisne or a cislo evidencni is said by Typ SO, and
                    they are different numbering series - a cislo evidencni 5
                    is not house number 5 - so the type is carried along
                    rather than flattened away.
    Cislo orientacni  the number on the blue plate, which counts along the
                    street. Filled for 83.8%. This is the half people say out
                    loud: "Jirska 3" is the orientacni.

Both are kept. The brief asked for cislo popisne, but an address written with
only the popisne is not the address anyone uses to find the door.

WHY THE WHOLE CITY

bezrealitky is swept city-wide every night, so listings arrive from all of
Prague, not only from the two watched boroughs. 134,627 rows is 3 MB gzipped
and the match is a dictionary lookup per street, so narrowing it would save
nothing worth the risk of a listing falling outside the narrowing.

WHAT THIS DOES NOT DO

It does not decide any listing's house number. It writes the register down;
common/ruian.py does the matching, and the distance it records is what says
how much to believe the result.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import zipfile
from datetime import date, timedelta
from typing import Iterator, Optional

from common import address as address_mod
from common import cas, krovak, net

PRAHA_OBEC_CODE = 554782
BASE = "https://vdp.cuzk.cz/vymenny_format/csv"

# Confirmed by tools/probe_ruian.py on 2026-09-21, against the real file:
# windows-1250, semicolon-separated, one member per zip.
ENCODING = "windows-1250"
DELIMITER = ";"

# The export is dated and today's date 404s - the probe checked. The file
# published is the previous month's end, so this walks month-ends backwards.
# A year of them: further back means the service changed, not that the date
# was stale, and a silently ancient index is worse than a failure.
MONTHS_BACK = 13

# Which of the two Czech numbering series a row belongs to, as Typ SO spells
# it. Carried into the index verbatim rather than mapped to a flag, because
# the third value the register uses is an empty string and a flag would have
# to guess what that meant.
TYP_CISLO_POPISNE = "č.p."

# What the index holds. Deliberately narrow: the columns the matcher reads,
# plus the register's own key so a row can be traced back to it.
INDEX_FIELDS = [
    "kod_adm",
    "ulice",            # folded to ASCII, which is what the match compares
    "ulice_original",   # as the register writes it, for display
    "cislo_domovni",
    "typ_cisla",        # "č.p." or "č.ev."
    "cislo_orientacni",
    "znak_orientacniho",
    "mestska_cast",     # Nazev MOMC: "Praha 11"
    "obvod",            # Nazev obvodu Prahy: "Praha 4" - the postal borough
    "cast_obce",        # Nazev casti obce: "Chodov" - the cadastral district
    "psc",
    "lat",
    "lon",
]


def candidate_dates(today: date) -> Iterator[date]:
    """Month-ends, newest first."""
    day = today
    for _ in range(MONTHS_BACK):
        end = day.replace(day=1) - timedelta(days=1)
        yield end
        day = end


def url_for(when: date) -> str:
    return f"{BASE}/{when:%Y%m%d}_OB_{PRAHA_OBEC_CODE}_ADR.csv.zip"


def download(session, today: Optional[date] = None) -> tuple:
    """(the date that answered, the CSV text). Raises if none did."""
    today = today or date.today()
    tried = []
    for when in candidate_dates(today):
        url = url_for(when)
        try:
            body = net.fetch_bytes(session, url, max_retries=2)
        except Exception as exc:                       # noqa: BLE001
            tried.append(f"{when:%Y-%m-%d} ({exc.__class__.__name__})")
            continue
        archive = zipfile.ZipFile(io.BytesIO(body))
        name = archive.namelist()[0]
        return when, archive.read(name).decode(ENCODING)

    raise RuntimeError(
        "no dated RUIAN export answered, over "
        f"{MONTHS_BACK} month-ends: {', '.join(tried)}")


def rows_from(text: str) -> Iterator[dict]:
    """The register's rows, converted, as index rows.

    Drops a row only for the two reasons that make it unusable: no surveyed
    point, or no street. Both are counted by the caller rather than silently
    absorbed - a jump in either means the export changed shape.
    """
    reader = csv.DictReader(io.StringIO(text), delimiter=DELIMITER)
    for row in reader:
        point = krovak.parse_point(row.get("Souřadnice Y"),
                                   row.get("Souřadnice X"))
        if point is None:
            continue
        street = (row.get("Název ulice") or "").strip()
        if not street:
            # 0.7% of Prague rows: addresses in a district that numbers its
            # houses without street names at all. Nothing can match them by
            # street, which is the only key the listings offer.
            continue
        lat, lon = point
        yield {
            "kod_adm": (row.get("Kód ADM") or "").strip(),
            "ulice": address_mod.strip_diacritics(street).lower(),
            "ulice_original": street,
            "cislo_domovni": (row.get("Číslo domovní") or "").strip(),
            "typ_cisla": (row.get("Typ SO") or "").strip(),
            "cislo_orientacni": (row.get("Číslo orientační") or "").strip(),
            "znak_orientacniho": (
                row.get("Znak čísla orientačního") or "").strip(),
            "mestska_cast": (row.get("Název MOMC") or "").strip(),
            "obvod": (row.get("Název obvodu Prahy") or "").strip(),
            "cast_obce": (row.get("Název části obce") or "").strip(),
            "psc": (row.get("PSČ") or "").strip(),
            "lat": f"{lat:.6f}",
            "lon": f"{lon:.6f}",
        }


def meta_path_for(out_path: str) -> str:
    """The sidecar beside the index. Named from the index rather than fixed,
    so a build to a different path cannot leave the two describing different
    files."""
    return out_path + ".json"


def write_meta(out_path: str, export_date, rows: int) -> str:
    """What this index was built from.

    The index itself cannot say which month's export it came from - it is
    just address points - so tools/ruian_due.py would have nothing to ask,
    and "is the index current" would become "when did the workflow last run",
    which is the question this project has repeatedly been wrong to trust.
    """
    path = meta_path_for(out_path)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"export": f"{export_date:%Y-%m-%d}",
                   "rows": rows,
                   # The day the build ran, in Prague. Lets the due check
                   # allow one attempt a day when the newest export has not
                   # been published yet, instead of every attempt in the
                   # window re-downloading the same file.
                   "built_at": cas.today().isoformat()},
                  handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def write_index(rows, out_path: str) -> int:
    """Write the index, byte-identical for identical content.

    Not gzip.open: it stamps the current time into the gzip header, so the
    file differs on every rebuild even when nothing in the register changed -
    and this is a 2.5 MB blob committed to a git repository. The first three
    real builds each committed a fresh copy while reporting "0 rows would
    change", which is the same waste the day-granular last_seen_at exists to
    avoid, at monthly instead of hourly cadence.

    With mtime=0 an unchanged register produces no diff at all and
    commit_data.sh correctly finds nothing to commit.
    """
    written = 0
    with open(out_path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS,
                                        extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    writer.writerow(row)
                    written += 1
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True,
                        help="where to write the gzipped index")
    args = parser.parse_args(argv)

    session = net.build_session()
    when, text = download(session)
    print(f"RUIAN export {when:%Y-%m-%d}: {len(text)} characters")

    rows = list(rows_from(text))
    total_lines = text.count("\n") - 1
    print(f"{total_lines} rows in the export, {len(rows)} usable "
          f"({total_lines - len(rows)} without a point or without a street)")

    if not rows:
        print("nothing usable in the export; refusing to write an empty index")
        return 1

    # The sanity check that costs nothing: everything must land in Prague.
    # A datum or axis error would put the whole cloud somewhere else, and an
    # index written anyway would then quietly match nothing for ever.
    lats = [float(r["lat"]) for r in rows]
    lons = [float(r["lon"]) for r in rows]
    print(f"lat {min(lats):.4f}..{max(lats):.4f}  "
          f"lon {min(lons):.4f}..{max(lons):.4f}")
    if not (49.9 < min(lats) and max(lats) < 50.2
            and 14.2 < min(lons) and max(lons) < 14.8):
        print("the converted points are not inside Prague; refusing to write")
        return 1

    written = write_index(rows, args.out)
    print(f"wrote {written} rows to {args.out}")
    meta = write_meta(args.out, when, written)
    print(f"wrote {meta}")

    popisne = sum(1 for r in rows if r["typ_cisla"] == TYP_CISLO_POPISNE)
    orientacni = sum(1 for r in rows if r["cislo_orientacni"])
    print(f"  {popisne} cislo popisne, {len(rows) - popisne} other type")
    print(f"  {orientacni} with a cislo orientacni")
    print(f"  {len(set(r['ulice'] for r in rows))} distinct streets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
