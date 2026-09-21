"""Write browsable CSV views of the dataset into data/csv/.

The three storage layers are normalized for correctness, not for reading:
to see "what is on the market right now, and for how much" you have to join
listings.csv against the newest row per listing in observations/. This
script does that join once per run and drops the result into data/csv/, so
the repository always contains a straightforward, openable answer:

    data/csv/aktivni_inzeraty.csv   currently on the market, newest price
    data/csv/priority_zona.csv      the same, but only Sporilov + Hostivar
    data/csv/vse_vcetne_zmizelych.csv  every listing ever seen, incl. removed
    data/csv/souhrn.csv             one-line-per-category summary counts

These are derived views - throwing them away and re-running this script
reproduces them exactly. The source of truth stays listings.csv +
observations/.

Run: python -m tools.export_csv
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from common import storage
from common.schema import STATUS_ACTIVE
from tools.listing_query import (
    as_bool,
    enrich,
    is_live,
    load_latest_prices,
    load_listings,
    write_csv,
)

VIEW_FIELDS = [
    "internal_id",
    "source",
    "source_id",
    "property_type",
    "transaction_type",
    "disposition",
    "area_m2",
    "price",
    "price_per_m2",
    "floor",
    # No `address` here on purpose. It stays in listings.csv - dedup.py
    # matches on it in four places, and it is the evidence ulice /
    # mestska_cast / obec are parsed from - but it is not what a person
    # reads: it is whichever shape the portal happened to use, so a column
    # of them sorts by the portal rather than by the street.
    "ulice",
    "cislo_popisne",
    # These travel WITH the number, always. cislo_popisne is inferred from
    # the address register, not published by any portal, and a column of
    # house numbers with nothing beside it reads as fact. See common/ruian.py.
    "cislo_orientacni",
    "cislo_typ",
    "cislo_zdroj",
    "cislo_vzdalenost_m",
    "cislo_kandidatu",
    "mestska_cast",
    "obec",
    "priority_zone",
    "status",
    "first_seen_at",
    "last_seen_at",
    "price_observed_at",
    "cluster_id",
    "dedup_confidence",
    "relisted_from",
    "lat",
    "lon",
    "gps_zdroj",
    "url",
]


def export(data_dir: Path = storage.DATA_DIR) -> dict[str, int]:
    listings = load_listings(data_dir)
    rows = enrich(listings, load_latest_prices(data_dir))
    out_dir = data_dir / "csv"

    live = [r for r in rows if is_live(r)]
    priority = [r for r in live if as_bool(r.get("priority_zone"))]

    def sort_key(row: dict) -> tuple:
        # By the parsed fields, not the raw address: the raw one sorts by
        # whichever shape the portal happened to use, so the same street
        # lands in three places depending on who advertised it.
        return (row.get("property_type", ""), row.get("transaction_type", ""),
                row.get("obec", ""), row.get("mestska_cast", ""),
                row.get("ulice", ""), row.get("address", ""))

    write_csv(sorted(live, key=sort_key), out_dir / "aktivni_inzeraty.csv", VIEW_FIELDS)
    write_csv(sorted(priority, key=sort_key), out_dir / "priority_zona.csv", VIEW_FIELDS)
    write_csv(sorted(rows, key=sort_key), out_dir / "vse_vcetne_zmizelych.csv", VIEW_FIELDS)

    counts = Counter(
        (r.get("source", ""), r.get("property_type", ""), r.get("transaction_type", ""), r.get("status", ""))
        for r in rows
    )
    summary = [
        {
            "source": source,
            "property_type": property_type,
            "transaction_type": transaction_type,
            "status": status,
            "count": count,
        }
        for (source, property_type, transaction_type, status), count in sorted(counts.items())
    ]
    write_csv(
        summary,
        out_dir / "souhrn.csv",
        ["source", "property_type", "transaction_type", "status", "count"],
    )

    return {
        "total": len(rows),
        "live": len(live),
        "active": sum(1 for r in rows if r.get("status") == STATUS_ACTIVE),
        "priority_zone": len(priority),
    }


def main() -> int:
    # --data-dir because the dataset is no longer always beside the code: the
    # scrape workflows run from the public repository with the private data
    # repository checked out into store/.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(storage.DATA_DIR))
    args = parser.parse_args()

    stats = export(Path(args.data_dir))
    print(
        "[export_csv] data/csv/ updated: "
        f"{stats['total']} listings total, {stats['live']} on the market "
        f"({stats['active']} active), {stats['priority_zone']} in the priority zone"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
