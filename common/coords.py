"""Lend coordinates to a listing from another advert for the same flat.

iDNES publishes no coordinates at all - 6,962 of the 10,967 adverts collected
- while sreality and bezrealitky publish them for every listing. Dedup
already knows which adverts are the same flat, so for a quarter of those
iDNES rows the coordinates are sitting in the cluster next to them: 1,859 of
6,985 GPS-less rows, taking coverage from 36% to 53% without a single
request.

Borrowed, not observed, and the two are not the same claim. `gps_zdroj`
records which it is - "" when the portal gave them, "cluster" when another
advert in the same cluster did - so nothing downstream has to assume, and a
map drawn from these can say which pins are second-hand.
"""

from __future__ import annotations

from typing import Optional

OWN = ""
FROM_CLUSTER = "cluster"


def _has_coords(row: dict) -> bool:
    return bool(str(row.get("lat") or "").strip()
                and str(row.get("lon") or "").strip())


def lend_within_clusters(listings: dict[str, dict]) -> int:
    """Fill blank lat/lon from a clustered sibling. Returns rows filled.

    A row that has its own coordinates is never touched: an advert's own
    figure beats a neighbour's, even when they disagree - and when they
    disagree that is worth keeping, not averaging away.
    """
    by_cluster: dict[str, list[dict]] = {}
    for row in listings.values():
        cluster_id = str(row.get("cluster_id") or "").strip()
        if cluster_id:
            by_cluster.setdefault(cluster_id, []).append(row)

    filled = 0
    for members in by_cluster.values():
        donor = next((m for m in members
                      if _has_coords(m) and not m.get("gps_zdroj")), None)
        if donor is None:
            continue
        for row in members:
            if _has_coords(row):
                continue
            row["lat"] = donor["lat"]
            row["lon"] = donor["lon"]
            row["gps_zdroj"] = FROM_CLUSTER
            filled += 1
    return filled
