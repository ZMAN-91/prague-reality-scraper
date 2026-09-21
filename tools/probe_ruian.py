"""What does RUIAN actually publish for Prague's addresses?

    python -m tools.probe_ruian      (needs internet; run it on a runner)

The listings carry a street and, for 5,775 of them, GPS coordinates - but
never a house number, because the portals do not publish one. The state's
address register does. RUIAN's public exchange format (VDP) offers a CSV of
every address point in a municipality: street, cislo domovni, cislo
orientacni, post code and a coordinate.

Everything about that sentence is hearsay until a runner has seen the file.
This probe writes nothing and decides nothing. It asks three questions and
prints the answers:

  1. WHICH URL EXISTS. The export is dated and the date is not announced
     anywhere machine-readable, so this walks candidate dates backwards from
     today and reports every one that answers with a zip.
  2. WHAT THE COLUMNS ARE. Printed verbatim, header and three sample rows,
     so the parser is written against the real thing.
  3. WHAT THE COORDINATES ARE. The numbers are printed raw and their range
     reported. S-JTSK puts Prague near X=-1043000, Y=-742000; WGS84 would be
     near 50.08, 14.44. The reader can tell which at a glance, and so can a
     later test - guessing this wrong puts every house number in the country
     somewhere else.

WHY NOT JUST GEOCODE

A geocoding API would answer per listing, which is 5,775 requests that must
be repeated as listings appear, against someone's rate limit and terms. The
CSV is one download of a public dataset, after which the matching is local
and free. It also comes from the register itself rather than from someone's
copy of it.
"""

from __future__ import annotations

import csv
import io
import sys
import zipfile
from datetime import date, timedelta

from common import net

# Praha, in the register's own numbering (kod obce). Printed by the probe
# from the file's own contents too, so a wrong value here shows up as an
# empty file rather than as somebody else's town.
PRAHA_OBEC_CODE = 554782

BASE = "https://vdp.cuzk.cz/vymenny_format/csv"

# How many month-ends to try. The export is published monthly; more than a
# year back would mean the service has changed, not that the date is old.
MONTHS_BACK = 14


def candidate_dates(today: date) -> list:
    """Month-ends, newest first, plus today - the two shapes VDP has used."""
    seen = []
    day = today
    for _ in range(MONTHS_BACK):
        # The last day of the month before `day`.
        first = day.replace(day=1)
        end = first - timedelta(days=1)
        seen.append(end)
        day = end
    return [today] + seen


def url_for(when: date) -> str:
    return f"{BASE}/{when:%Y%m%d}_OB_{PRAHA_OBEC_CODE}_ADR.csv.zip"


def probe() -> int:
    session = net.build_session()

    # Robots first, and reported on its own line. A disallowed path and a
    # missing file both end the probe, but they are opposite answers: one
    # says the data is not to be taken this way, the other that the URL is
    # wrong. Rolled together they would read as "RUIAN does not have it".
    print("== 0. robots.txt")
    probe_url = url_for(date.today())
    try:
        allowed = net.is_allowed_by_robots(session, probe_url)
    except Exception as exc:                          # noqa: BLE001
        print(f"  could not be read: {type(exc).__name__}: {exc}")
    else:
        print(f"  {probe_url}")
        print(f"  allowed: {allowed}")
        if not allowed:
            print("\n  Disallowed. Stopping - the register publishes this as "
                  "an open dataset, so if this really is a Disallow the "
                  "answer is to ask CUZK, not to fetch it anyway.")
            return 1

    found = None
    print("\n== 1. which dated export answers")
    for when in candidate_dates(date.today()):
        url = url_for(when)
        try:
            body = net.fetch_bytes(session, url, max_retries=1)
        except Exception as exc:                      # noqa: BLE001
            print(f"  {when:%Y-%m-%d}  no   ({type(exc).__name__}: {exc})")
            continue
        print(f"  {when:%Y-%m-%d}  YES  {len(body)} bytes")
        found = (when, body)
        # One hit is the answer. Walking on would only ask the service for
        # files this project will never use.
        break

    if found is None:
        print("\nNo dated export answered, over "
              f"{MONTHS_BACK + 1} candidate dates. The URL shape is wrong or "
              "the service moved; nothing below can be inferred.")
        return 1

    when, body = found
    print(f"\n== 2. inside {url_for(when)}")
    archive = zipfile.ZipFile(io.BytesIO(body))
    names = archive.namelist()
    print(f"  members: {names}")

    raw = archive.read(names[0])
    print(f"  {names[0]}: {len(raw)} bytes uncompressed")

    # The encoding is part of what is being asked; try the two that ARE used
    # for Czech public data and say which one read cleanly.
    text = None
    for encoding in ("windows-1250", "utf-8", "utf-8-sig"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            print(f"  encoding {encoding}: no")
            continue
        print(f"  encoding {encoding}: decodes")
        break
    if text is None:
        print("  no candidate encoding decoded the file")
        return 1

    lines = text.splitlines()
    print(f"  {len(lines)} lines")

    print("\n== 3. the header, verbatim")
    print(f"  {lines[0]!r}")

    delimiter = ";" if lines[0].count(";") > lines[0].count(",") else ","
    print(f"  delimiter looks like {delimiter!r}")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    columns = reader.fieldnames or []
    print(f"  {len(columns)} columns:")
    for name in columns:
        print(f"    {name!r}")

    print("\n== 4. three rows, verbatim")
    rows = []
    for index, row in enumerate(reader):
        if index < 3:
            print(f"  --- row {index}")
            for name in columns:
                print(f"    {name!r}: {row.get(name)!r}")
        rows.append(row)
        if index > 200000:
            break
    print(f"\n  {len(rows)} data rows")

    print("\n== 5. the numeric columns, and their range")
    # Which columns hold numbers at all, and what those numbers look like.
    # Reported for every column rather than the ones expected, because the
    # point is to find out which pair is the coordinate.
    for name in columns:
        values = []
        for row in rows[:5000]:
            text_value = (row.get(name) or "").replace(",", ".").strip()
            try:
                values.append(float(text_value))
            except ValueError:
                continue
        if len(values) < 100:
            continue
        low, high = min(values), max(values)
        print(f"  {name!r}: {len(values)} numeric, {low} .. {high}")

    print("\n  For reference: Prague in S-JTSK/Krovak (EPSG:5514) is around")
    print("  X -1040000, Y -740000; in WGS84 around lat 50.08, lon 14.44.")

    print("\n== 6. how complete is the house number")
    if rows:
        for name in columns:
            filled = sum(1 for row in rows if (row.get(name) or "").strip())
            print(f"  {name!r}: {filled}/{len(rows)} filled "
                  f"({100.0 * filled / len(rows):.1f}%)")

    return 0


if __name__ == "__main__":
    sys.exit(probe())
