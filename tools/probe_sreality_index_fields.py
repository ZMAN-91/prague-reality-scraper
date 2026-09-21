"""What is actually in a sreality index row, field by field?

    python -m tools.probe_sreality_index_fields   (needs internet; on a runner)

One request. Prints the keys of the first rows and the values of everything
that might carry a floor area, verbatim.

WHY

parse_estate reads the area from `usable_area`. A city-wide donor walk showed
that field is empty on every index row: 10,246 adverts, not one with an area,
which made every candidate fail the size test and lent zero coordinates.

Disposition survives the same journey because `_disposition` falls back to
reading `advert_name` when the enum is missing. Nobody wrote that fallback
for area. Before writing one, this asks what the title actually looks like -
the module has twice now carried a confident claim about index rows that the
rows themselves did not support.
"""

from __future__ import annotations

import json
import sys

from common import net
from scrapers.sreality import (CATEGORY_MAIN, CATEGORY_TYPE, COUNTRY_ID,
                               DISTRICT_PRAHA, INDEX_URL)

# Anything whose name hints at a size, plus the title fields.
INTERESTING = ("usable_area", "area", "floor_area", "built_up_area",
               "garden_area", "total_area", "advert_name", "name",
               "labels", "labelsAll", "seo")


def probe() -> int:
    session = net.build_session()
    payload = net.fetch_json(session, INDEX_URL, params={
        "category_main_cb": CATEGORY_MAIN["byt"],
        "category_type_cb": CATEGORY_TYPE["prodej"],
        "locality_country_id": COUNTRY_ID,
        "locality_district_id": DISTRICT_PRAHA,
        "limit": 5,
        "offset": 0,
    })
    rows = (payload.get("results") or [])
    print(f"{len(rows)} rows returned\n")
    if not rows:
        print("nothing came back; nothing can be concluded")
        return 1

    print("== every key on the first row")
    for key in sorted(rows[0]):
        print(f"  {key}")

    print("\n== the fields that might carry a size, for each row")
    for index, row in enumerate(rows):
        print(f"\n  --- row {index}")
        for key in INTERESTING:
            if key in row:
                value = row[key]
                text = json.dumps(value, ensure_ascii=False)
                print(f"    {key!r}: {text[:220]}")
        # Anything else numeric that could plausibly be square metres.
        for key, value in sorted(row.items()):
            if key in INTERESTING:
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if 10 <= value <= 500:
                    print(f"    (numeric in a plausible m2 range) {key!r}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(probe())
