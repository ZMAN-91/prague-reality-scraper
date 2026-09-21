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

SUGGEST_BASE = "https://www.sreality.cz/api/v1/localities/suggest?phrase="

# The watched area, named the way a person would type it. Round one guessed
# the ids and every guess was ignored, which looked exactly like "the API
# cannot do this" - so the ids come from sreality's own autocomplete now.
PHRASES = ["Praha 4", "Praha 10", "Praha 11", "Praha 15",
           "Hostivar", "Chodov", "Zabehlice", "Sporilov"]

# Parameter names to try with a real id. The autocomplete calls Praha 10 an
# entityType "quarter" under category "quarter_cz", so quarter_id is the
# first guess - but the name is still a guess and the totals decide.
PARAM_NAMES = ("locality_quarter_id", "locality_ward_id", "locality_region_id")


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


def suggest(session, phrase: str) -> list[dict]:
    """What sreality's own autocomplete knows about a place name."""
    from urllib.parse import quote
    try:
        payload = net.fetch_json(session, SUGGEST_BASE + quote(phrase))
    except Exception as exc:  # noqa: BLE001
        print(f"    {phrase:<12} selhalo: {str(exc)[:90]}")
        return []
    found = []
    for row in (payload.get("results") or [])[:3]:
        data = row.get("userData") or {}
        found.append({"phrase": phrase, "id": data.get("id"),
                      "entityType": data.get("entityType"),
                      "category": row.get("category"),
                      "label": data.get("suggestFirstRow"),
                      "second": data.get("suggestSecondRow"),
                      "lat": data.get("latitude"), "lon": data.get("longitude")})
    for row in found:
        print(f"    {phrase:<12} id={row['id']:<6} {row['entityType']:<10} "
              f"{row['label']}  |  {row['second']}")
    net.polite_sleep()
    return found


def main() -> int:
    session = net.build_session()

    print("=" * 72)
    print("1. Co zna sreality vlastni naseptavac")
    print("=" * 72)
    known = []
    for phrase in PHRASES:
        known.extend(suggest(session, phrase))

    print("\n" + "=" * 72)
    print("2. Filtr se skutecnym id")
    print("=" * 72)
    control = base_params()
    ask(session, "kontrola: cela Praha", control)

    # One place, every candidate parameter name: the name is what is unknown
    # now, not the value.
    praha10 = next((r for r in known
                    if r["phrase"] == "Praha 10" and r["id"]), None)
    if praha10:
        for name in PARAM_NAMES:
            params = base_params()
            params[name] = praha10["id"]
            ask(session, f"{name}={praha10['id']} (Praha 10)", params)
    else:
        print("  naseptavac nevratil id pro Prahu 10 - nelze zkusit")

    # Then, with whatever name worked, the rest of the watched area. Printed
    # even if the name is still wrong: four identical totals say so plainly.
    print("\n" + "=" * 72)
    print("3. Sledovana oblast po mestskych castech")
    print("=" * 72)
    for row in known:
        if row["entityType"] != "quarter" or not row["id"]:
            continue
        params = base_params()
        params["locality_quarter_id"] = row["id"]
        ask(session, f"{row['label']} (id={row['id']})", params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
