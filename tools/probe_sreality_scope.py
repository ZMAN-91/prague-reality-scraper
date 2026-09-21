"""Can sreality's API be asked for one part of Prague, the way iDNES can?

    python -m tools.probe_sreality_scope    (needs internet; run it on a runner)

iDNES turned out to take a borough in the search path, so the hourly pass can
ask it for the watched area instead of walking the city. sreality is filtered
by coordinates after the fact instead, which means every hourly pass still
pulls the whole Prague index and throws ~92% of it away: 8,341 rows fetched,
7,658 discarded, on every run.

The API takes locality_district_id=47 for Prague. Whether it takes anything
finer is the question, and there are two shapes worth asking for:

  - a borough id, to match what iDNES does. sreality's own autocomplete is
    the only way to learn the parameter name and the value, so this asks it.
  - a bounding box. Better than a borough if it exists, because the watched
    area is a shape, not an administrative unit - it is defined by a belt
    between named places and no post code matches it.

Read-only: a handful of requests, reading `pagination.total` and the
localities that come back. Nothing is written.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from urllib.parse import urlencode

from common import net
from scrapers.sreality import CATEGORY_MAIN, CATEGORY_TYPE, COUNTRY_ID, INDEX_URL

PRAHA_DISTRICT_ID = 47

# The watched area's corners, from common/collection_area - Sporilov in the
# north-west to Horni Mecholupy in the south-east, with slack.
AREA_BOX = {"north": 50.075, "south": 50.010, "west": 14.440, "east": 14.600}

SUGGEST_URLS = [
    "https://www.sreality.cz/api/v1/localities/suggest?phrase=Praha%2010",
    "https://www.sreality.cz/api/cs/v2/suggest?phrase=Praha%2010",
    "https://www.sreality.cz/api/v1/suggest?phrase=Praha%2010",
    "https://www.sreality.cz/api/v1/localities?phrase=Hostiva%C5%99",
]


def base_params() -> dict:
    return {
        "category_main_cb": CATEGORY_MAIN["byt"],
        "category_type_cb": CATEGORY_TYPE["prodej"],
        "locality_country_id": COUNTRY_ID,
        "locality_district_id": PRAHA_DISTRICT_ID,
        "per_page": 20,
    }


def ask(session, label: str, params: dict) -> None:
    url = f"{INDEX_URL}?{urlencode(params)}"
    print(f"\n--- {label}\n    {url}")
    try:
        payload = net.fetch_json(session, url)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        print(f"    selhalo: {str(exc)[:160]}")
        return
    total = (payload.get("pagination") or {}).get("total")
    results = payload.get("results") or []
    print(f"    total = {total}, vraceno {len(results)}")
    cities = Counter()
    for row in results[:20]:
        loc = row.get("locality") or {}
        cities[f"{loc.get('citypart') or loc.get('city') or '?'}"] += 1
    print(f"    casti: {', '.join(f'{c}={n}' for c, n in cities.most_common(8))}")
    net.polite_sleep()


def main() -> int:
    session = net.build_session()

    print("=" * 72)
    print("1. Co zna sreality vlastni naseptavac")
    print("=" * 72)
    for url in SUGGEST_URLS:
        print(f"\n--- {url}")
        try:
            payload = net.fetch_json(session, url)
        except Exception as exc:  # noqa: BLE001
            print(f"    selhalo: {str(exc)[:120]}")
            continue
        text = json.dumps(payload, ensure_ascii=False)
        print(f"    odpoved ({len(text)} znaku): {text[:700]}")
        net.polite_sleep()

    print("\n" + "=" * 72)
    print("2. Filtry, kontrola vs kandidati")
    print("=" * 72)
    ask(session, "kontrola: cela Praha", base_params())

    # Candidate parameter names, each with a plausible Praha 10 value. A name
    # the API does not know is normally ignored, so the tell is `total`: a
    # filter that bit returns fewer than the control, one that was ignored
    # returns exactly the control's number.
    for name, value in (("locality_ward_id", 5087),
                        ("locality_quarter_id", 5087),
                        ("locality_municipality_id", 5087),
                        ("locality_region_id", 10)):
        params = base_params()
        params[name] = value
        ask(session, f"kandidat {name}={value}", params)

    box = base_params()
    box.update({"map_bounds_north": AREA_BOX["north"], "map_bounds_south": AREA_BOX["south"],
                "map_bounds_west": AREA_BOX["west"], "map_bounds_east": AREA_BOX["east"]})
    ask(session, "bounding box (map_bounds_*)", box)

    tiles = base_params()
    tiles["map"] = (f"{AREA_BOX['west']},{AREA_BOX['south']}|"
                    f"{AREA_BOX['east']},{AREA_BOX['north']}")
    ask(session, "bounding box (map=w,s|e,n)", tiles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
