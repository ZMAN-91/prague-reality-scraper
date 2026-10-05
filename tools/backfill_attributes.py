"""Fill the attribute columns of stored listings from the raw archive.

    python -m tools.backfill_attributes --data-dir store/data [--apply]

The columns of common/attributes.py were added on 2026-10-05, but what they
hold arrived with every advert from the first day and is still in
data/raw/: the detail payload of every bezrealitky advert, sreality's daily
index walk and every sreality detail fetched. This reads those, newest last
so the latest statement of each advert wins, and fills each stored row.

Only empty cells are filled for what a seller edits (construction,
ownership, condition, charges...): a value already stored came from a later
sighting than anything in the archive could. The portal's circumstances
(discount badge, original price, seller) are set from the newest archived
payload when the row has none.

Dry run by default. Run it under the scrape-data lock (maintenance.yml).
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
from pathlib import Path

from common import attributes, storage
from scrapers import bezrealitky, sreality


def _payloads(path: Path):
    try:
        payload = json.load(gzip.open(path))
    except (OSError, ValueError):
        return []
    return payload.get("pages", []) if isinstance(payload, dict) else []


def archived_attributes(data_dir: Path) -> dict:
    """(source, source_id) -> attributes, merged oldest to newest so that
    later payloads win and empty values never erase earlier ones."""
    found: dict = {}

    def take(key, values):
        merged = found.setdefault(key, {})
        for name, value in values.items():
            if value not in ("", None):
                merged[name] = value

    raw = data_dir / "raw"
    # Sorted by day directory then file, which is chronological.
    for path in sorted((raw / bezrealitky.SOURCE_NAME).glob("*/detail-*.json.gz")):
        for page in _payloads(path):
            advert = page.get("response") if isinstance(page, dict) else None
            if isinstance(advert, dict) and advert.get("id") is not None:
                take((bezrealitky.SOURCE_NAME, str(advert["id"])), attributes.from_bezrealitky(advert))

    for path in sorted((raw / sreality.SOURCE_NAME).glob("*/*.json.gz")):
        for page in _payloads(path):
            if not isinstance(page, dict):
                continue
            response = page.get("response") or {}
            if page.get("kind") == "detail":
                estate = response.get("result") if isinstance(response.get("result"), dict) else response
                source_id = page.get("source_id") or page.get("detail_source_id")
                if isinstance(estate, dict) and source_id:
                    take((sreality.SOURCE_NAME, str(source_id)), attributes.from_sreality(estate))
            else:
                for item in response.get("results") or []:
                    if isinstance(item, dict) and item.get("hash_id") is not None:
                        take((sreality.SOURCE_NAME, str(item["hash_id"])), attributes.from_sreality(item))
    return found


def fill(listings: dict, archived: dict) -> collections.Counter:
    filled = collections.Counter()
    for row in listings.values():
        values = archived.get((row.get("source"), row.get("source_id")))
        if not values:
            continue
        for name in attributes.FIELDS:
            value = values.get(name)
            if value in ("", None) or str(row.get(name) or "").strip():
                continue
            row[name] = value
            filled[name] += 1
    return filled


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    archived = archived_attributes(data_dir)
    listings = storage.read_listings(data_dir / "listings.csv")
    filled = fill(listings, archived)
    by_source = collections.Counter(r["source"] for r in listings.values()
                                    if (r["source"], r["source_id"]) in archived)
    print(f"{len(archived)} adverts in the raw archive; rows with an archived "
          f"payload by source: {dict(by_source)}")
    for name in attributes.FIELDS:
        have = sum(1 for r in listings.values() if str(r.get(name) or "").strip())
        print(f"  {name:14s} filled now {filled[name]:6d}   rows with a value {have:6d} "
              f"({100 * have / max(len(listings), 1):.0f} %)")
    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0
    storage.write_listings(listings, data_dir / "listings.csv")
    print(f"Wrote {sum(filled.values())} values.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
