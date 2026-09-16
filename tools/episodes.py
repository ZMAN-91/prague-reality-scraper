"""One row per time a physical property was on the market.

The three storage layers are correct but they are not answers. "Which flats
vanished within a week, and for how much" or "which were listed three months
or more, and how did they discount" are both several joins away, and each one
has the same four traps in it. Measured on the live data, forgetting just the
first of them moves the average price of a one-week disappearance by 6.3%.

    1. cluster_id groups duplicates, it does not remove them. The same flat
       advertised by three agencies is three rows in listings.csv. Count rows
       and you count it three times.

    2. relisted_from is the whole of the "three months on the market"
       question. Sellers delist and relist to look fresh; that is the normal
       case, not an edge case. Ignore it and a flat that spent nine months
       being sold looks like three unrelated one-month listings - and the
       long-listing analysis, which is specifically about those properties,
       silently excludes exactly them.

    3. last_seen_at is when it was last *seen*, which is what "how long was
       it on the market" wants. The `removed` status arrives later, after
       three consecutive misses, and for iDNES later still because absence
       is judged per sweep. Measuring to the removal would add days of lag
       that vary by source.

    4. A removal writes an observation with an empty price. The last price is
       the last non-empty one, not the last one.

So this file encodes those four once, in tested code, instead of in every
analysis anyone ever writes. What comes out is flat and boring on purpose:

    python -m tools.episodes                 -> data/csv/historie_nemovitosti.csv

An *episode* is one continuous period during which a property was being
advertised, across however many portals and however many re-listings. Two
adverts belong to the same episode if they are the same property and the gap
between them is at most GAP_DAYS; a longer gap is a new attempt to sell,
which is a different thing and should not be averaged with the first.

WHAT THIS CANNOT TELL YOU: whether a listing that disappeared was sold or
withdrawn. No portal publishes that, so neither does this. `outcome` says
removed, never sold.
"""

from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from common import storage
from common.schema import STATUS_ACTIVE

# A property re-advertised within two weeks is still the same attempt to
# sell; after longer, the seller has regrouped and it is a new one. The
# output carries gap_before_days so anyone who disagrees can re-segment
# without re-deriving any of the rest.
GAP_DAYS = 14

FIELDS = [
    "property_key", "episode",
    "property_type", "transaction_type", "disposition", "area_m2",
    "address", "priority_zone",
    "sources", "listing_count", "internal_ids",
    "first_seen", "last_seen", "days_on_market", "gap_before_days",
    "outcome",
    "first_price", "last_price", "min_price", "max_price",
    "price_changes", "discount_czk", "discount_pct",
    "price_per_m2_first", "price_per_m2_last",
]


def as_date(value: Optional[str]) -> Optional[date]:
    """Both date and full-timestamp columns reduce to a day here: that is the
    resolution last_seen_at actually has (see README on why)."""
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_observations(data_dir: Path) -> dict[str, list[dict]]:
    """Every observation, keyed by listing, oldest first.

    Reads every monthly file: an episode spanning a year spans twelve of
    them, and a "three months or more" question is about exactly those.
    """
    by_id: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(glob.glob(str(data_dir / "observations" / "*.csv"))):
        with open(path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                by_id[row["internal_id"]].append(row)
    for rows in by_id.values():
        rows.sort(key=lambda r: r.get("observed_at") or "")
    return by_id


class Union:
    """Union-find over listing ids, so the two ways two adverts can be the
    same property - clustered duplicates and re-listings - end up in one
    group even when they chain: A relisted as B, B duplicated as C."""

    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Lowest id wins, so the key is stable across runs rather than
            # depending on which advert happened to be processed first.
            lo, hi = sorted((ra, rb))
            self.parent[hi] = lo


def group_properties(listings: dict[str, dict]) -> dict[str, str]:
    """internal_id -> property_key, merging duplicates and re-listings."""
    union = Union()
    for internal_id, row in listings.items():
        union.find(internal_id)
        cluster = (row.get("cluster_id") or "").strip()
        if cluster:
            union.join(internal_id, f"cluster:{cluster}")
        relisted = (row.get("relisted_from") or "").strip()
        if relisted and relisted in listings:
            union.join(internal_id, relisted)
    return {internal_id: union.find(internal_id) for internal_id in listings}


def price_series(rows: list[dict]) -> list[tuple[str, int]]:
    """(observed_at, price) for observations that carry a price.

    Trap 4: a removal records an empty price, which is not a price of zero
    and not the last price either.
    """
    out = []
    for row in rows:
        price = as_int(row.get("price"))
        if price is not None:
            out.append((row.get("observed_at") or "", price))
    return out


def segment(spans: list[tuple[date, date, str]], gap_days: int = GAP_DAYS):
    """Merge overlapping or near-touching (first_seen, last_seen) spans.

    Input is sorted by start. Yields (start, end, [ids], gap_before_days).
    """
    episodes = []
    for start, end, internal_id in sorted(spans):
        if episodes and start - episodes[-1]["end"] <= timedelta(days=gap_days):
            episodes[-1]["end"] = max(episodes[-1]["end"], end)
            episodes[-1]["ids"].append(internal_id)
        else:
            gap = (start - episodes[-1]["end"]).days if episodes else None
            episodes.append({"start": start, "end": end,
                             "ids": [internal_id], "gap": gap})
    return episodes


def build(listings: dict[str, dict], observations: dict[str, list[dict]],
          gap_days: int = GAP_DAYS) -> list[dict]:
    keys = group_properties(listings)
    by_property: dict[str, list[str]] = defaultdict(list)
    for internal_id, key in keys.items():
        by_property[key].append(internal_id)

    out: list[dict] = []
    for key, ids in sorted(by_property.items()):
        spans = []
        for internal_id in ids:
            row = listings[internal_id]
            # Trap 3: the span ends when it was last seen, not when it was
            # finally declared removed.
            start = as_date(row.get("first_seen_at"))
            end = as_date(row.get("last_seen_at"))
            if start and end and end >= start:
                spans.append((start, end, internal_id))
        if not spans:
            continue

        for index, episode in enumerate(segment(spans, gap_days), start=1):
            members = episode["ids"]
            rows = [listings[i] for i in members]

            series: list[tuple[str, int]] = []
            changes = 0
            for internal_id in members:
                own = price_series(observations.get(internal_id, []))
                series.extend(own)
                # Counted per advert, so two portals quoting slightly
                # different figures do not read as the price oscillating.
                changes += sum(1 for a, b in zip(own, own[1:]) if a[1] != b[1])
            series.sort()

            first_price = series[0][1] if series else None
            last_price = series[-1][1] if series else None
            prices = [p for _, p in series]

            area = next((as_float(r.get("area_m2")) for r in rows
                         if as_float(r.get("area_m2"))), None)
            live = any(r.get("status") == STATUS_ACTIVE for r in rows)

            discount = (first_price - last_price) if series else None
            out.append({
                "property_key": key,
                "episode": index,
                "property_type": rows[0].get("property_type", ""),
                "transaction_type": rows[0].get("transaction_type", ""),
                "disposition": next((r.get("disposition") for r in rows
                                     if r.get("disposition")), ""),
                "area_m2": area if area is not None else "",
                "address": next((r.get("address") for r in rows
                                 if r.get("address")), ""),
                "priority_zone": any(str(r.get("priority_zone")).lower() == "true"
                                     for r in rows),
                "sources": "|".join(sorted({r.get("source", "") for r in rows})),
                "listing_count": len(members),
                "internal_ids": "|".join(sorted(members)),
                "first_seen": episode["start"].isoformat(),
                "last_seen": episode["end"].isoformat(),
                "days_on_market": (episode["end"] - episode["start"]).days,
                "gap_before_days": "" if episode["gap"] is None else episode["gap"],
                "outcome": "active" if live else "removed",
                "first_price": first_price if first_price is not None else "",
                "last_price": last_price if last_price is not None else "",
                "min_price": min(prices) if prices else "",
                "max_price": max(prices) if prices else "",
                "price_changes": changes,
                "discount_czk": discount if discount is not None else "",
                "discount_pct": (round(100 * discount / first_price, 2)
                                 if series and first_price else ""),
                "price_per_m2_first": (round(first_price / area)
                                       if first_price and area else ""),
                "price_per_m2_last": (round(last_price / area)
                                      if last_price and area else ""),
            })
    return out


def export(data_dir: Path = storage.DATA_DIR, gap_days: int = GAP_DAYS) -> dict:
    listings = storage.read_listings(data_dir / "listings.csv")
    observations = load_observations(data_dir)
    rows = build(listings, observations, gap_days)

    out_dir = data_dir / "csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "historie_nemovitosti.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    return {
        "episodes": len(rows),
        "properties": len({r["property_key"] for r in rows}),
        "listings": len(listings),
        "with_price": sum(1 for r in rows if r["first_price"] != ""),
        "path": str(path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    parser.add_argument("--gap-days", type=int, default=GAP_DAYS,
                        help="a longer gap starts a new episode")
    args = parser.parse_args()

    stats = export(Path(args.data_dir), args.gap_days)
    print(f"[episodes] {stats['path']}: {stats['episodes']} episodes across "
          f"{stats['properties']} properties, from {stats['listings']} adverts "
          f"({stats['with_price']} with a price)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
