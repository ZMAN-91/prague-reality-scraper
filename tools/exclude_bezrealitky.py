"""Remove the bezrealitky adverts the dataset no longer takes, once.

    python -m tools.exclude_bezrealitky --data-dir store/data [--apply]

Since 2026-10-05 the scraper leaves out FLATIO lets and anything the portal
marks as outside Prague (scrapers/bezrealitky.exclusion_of), and drops a
stored one the next time it reads its page. That reaches every advert still
in the sitemap within a run or two - but not the ones that had already left
it: removed and disappearing adverts are never read again, so they would stay
in the history for ever, counted in every past day's supply.

This decides from the raw archive instead, which holds the detail payload of
every bezrealitky advert ever read (data/raw/bezrealitky/*/detail-*.json.gz),
and removes every stored row the rule excludes, whatever its status. It also
seeds the scraper's memory of excluded adverts so none is read again.

Observation and change rows stay in their append-only logs, keyed to ids
nothing refers to any more; nothing reads them without a listing.

Dry run by default. Run it under the scrape-data lock (maintenance.yml): a
row removed here and still held by a concurrent run would be merged back.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import sys
from pathlib import Path

from common import progress as progress_state
from common import storage
from scrapers import bezrealitky


def excluded_from_raw(data_dir: Path) -> dict:
    """source_id -> reason, from every archived bezrealitky detail. The
    newest payload of an advert decides."""
    verdict: dict = {}
    for path in sorted((data_dir / "raw" / bezrealitky.SOURCE_NAME).glob("*/detail-*.json.gz")):
        try:
            payload = json.load(gzip.open(path))
        except (OSError, ValueError):
            continue
        for page in payload.get("pages", []) if isinstance(payload, dict) else []:
            advert = page.get("response") if isinstance(page, dict) else None
            if isinstance(advert, dict) and advert.get("id") is not None:
                verdict[str(advert["id"])] = bezrealitky.exclusion_of(advert)
    return {source_id: reason for source_id, reason in verdict.items() if reason}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    excluded = excluded_from_raw(data_dir)
    listings = storage.read_listings(data_dir / "listings.csv")
    doomed = [internal_id for internal_id, row in listings.items()
              if row.get("source") == bezrealitky.SOURCE_NAME
              and row.get("source_id") in excluded]
    counts = collections.Counter((excluded[listings[i]["source_id"]], listings[i]["status"])
                                 for i in doomed)
    print(f"{len(excluded)} excluded adverts in the raw archive; "
          f"{len(doomed)} stored rows to remove.")
    for (reason, status), n in sorted(counts.items()):
        print(f"  {n:6d}  {reason:12s} {status}")

    if not args.apply:
        print("Dry run; pass --apply to write.")
        return 0

    for internal_id in doomed:
        del listings[internal_id]
    storage.write_listings(listings, data_dir / "listings.csv")

    state_path = data_dir / "state" / "last_observation.json"
    state = storage.read_last_observation_state(state_path)
    for internal_id in doomed:
        state.pop(internal_id, None)
    storage.write_last_observation_state(state, state_path)

    progress = progress_state.read(data_dir)
    memory = progress.setdefault(bezrealitky.SOURCE_NAME, {}).setdefault("vyrazene", {})
    memory.update(excluded)
    progress_state.write(data_dir, progress)
    print(f"Removed {len(doomed)} rows; {len(memory)} adverts remembered as excluded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
