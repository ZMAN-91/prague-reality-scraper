"""Give a listing a house number, and say how much to believe it.

The portals publish a street and, for about half the listings, a coordinate.
They never publish a house number. The state address register publishes all
three, so the number can be recovered by asking: of the register's points on
this street, which is nearest to where the portal put the pin?

THIS IS AN ESTIMATE AND IS STORED AS ONE

The answer is only as good as the portal's pin. A pin on the building gives
the right door; a pin on the middle of the street gives whichever door is
nearest the middle of the street. Nothing in the data says which kind of pin
it is, and a number that is wrong looks exactly like a number that is right.

So the number is never stored on its own. Three fields travel with it:

    cislo_zdroj         "ruian" - this was inferred here, not published
    cislo_vzdalenost_m  how far the pin was from that address point
    cislo_kandidatu     how many different houses on the street were about
                        equally close

A row with cislo_vzdalenost_m of 4 and cislo_kandidatu of 1 is a building the
portal pinned exactly. A row with 90 and 6 is a street name and a shrug. Both
are useful; treating them as the same thing is not.

WHY NEAREST, AND NOT INTERPOLATION

House numbers along a Czech street are not a monotone sequence that can be
interpolated: cislo popisne is unique within a cadastral district and assigned
in the order houses were built, so consecutive plates on one street can read
1204, 87, 2310. Only the cislo orientacni counts along the street, and it is
missing for 16% of points. Nearest-point is the only method the data supports.

MATCHING IS BY STREET NAME, FOLDED

Both sides are stripped of diacritics and lowercased, so "Roháčova" from the
register meets "Rohacova" from iDNES. What it does not solve is a portal that
abbreviates - "nam." for "namesti" - which simply fails to match and is
counted as a miss rather than guessed at.
"""

from __future__ import annotations

import csv
import gzip
from collections import defaultdict
from typing import Dict, List, Optional

from common import address as address_mod
from common import geo

# Beyond this the pin says nothing about which door it is. Generous on
# purpose: the distance is recorded, so a caller who wants 30 m can filter,
# whereas a number thrown away here cannot be recovered. Measured against the
# real data by tools/backfill_cislo.py, which prints the distribution.
MAX_DISTANCE_M = 250.0

# How far past the nearest point another house still counts as a plausible
# alternative. Used only to count candidates, never to reject: the nearest is
# always reported, with the count saying how crowded the answer was.
#
# Proportional to the pin's own error, because that error is what decides
# whether two houses can be told apart - and it varies hugely. Measured over
# 5,652 matches the portals' pins land 3.1 m out at the median, 13.5 m at p75
# and 89.9 m at p99, and a quarter of them land within 0.1 m, which means
# those portals geocode from this same register.
#
# A fixed radius cannot serve both ends of that. At 25 m it called a pin
# sitting exactly on a building ambiguous with its neighbours 15 m away,
# which is not ambiguity, it is a terraced street - and it reported only
# 28.5% of matches as unambiguous when the median match is accurate to three
# metres. Twice the observed error, with a floor so that a pin landing dead
# on a point does not claim infinite precision.
AMBIGUITY_FLOOR_M = 5.0
AMBIGUITY_FACTOR = 2.0

MATCH_FIELDS = ("cislo_popisne", "cislo_orientacni", "cislo_zdroj",
                "cislo_vzdalenost_m", "cislo_kandidatu")

SOURCE_NAME = "ruian"


class Index:
    """The register's points, grouped by folded street name."""

    def __init__(self, by_street: Dict[str, List[dict]]):
        self.by_street = by_street

    def __len__(self) -> int:
        return sum(len(points) for points in self.by_street.values())

    @property
    def streets(self) -> int:
        return len(self.by_street)

    @classmethod
    def load(cls, path: str) -> "Index":
        """Read the gzipped index written by tools/build_ruian_index.py."""
        by_street: Dict[str, List[dict]] = defaultdict(list)
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                street = (row.get("ulice") or "").strip()
                if not street:
                    continue
                try:
                    row["lat"] = float(row["lat"])
                    row["lon"] = float(row["lon"])
                except (KeyError, TypeError, ValueError):
                    continue
                by_street[street].append(row)
        return cls(dict(by_street))

    def match(self, lat: Optional[float], lon: Optional[float],
              ulice: Optional[str],
              max_distance_m: float = MAX_DISTANCE_M) -> Optional[dict]:
        """The nearest register point on `ulice`, as the five match fields.

        None when there is nothing to go on: no coordinate, no street, a
        street the register does not know, or a nearest point too far away to
        mean anything.
        """
        found = self.match_detail(lat, lon, ulice, max_distance_m)
        return found[0] if found else None

    def match_detail(self, lat: Optional[float], lon: Optional[float],
                     ulice: Optional[str],
                     max_distance_m: float = MAX_DISTANCE_M) -> Optional[tuple]:
        """(the five match fields, the register row they came from).

        The row carries the district the register places that building in,
        which nothing in the matching uses - which is exactly what makes it
        worth checking against the district the portal stated. See
        tools/backfill_cislo.py.
        """
        if lat is None or lon is None or not ulice:
            return None

        key = address_mod.strip_diacritics(str(ulice)).lower().strip()
        points = self.by_street.get(key)
        if not points:
            return None

        scored = []
        for point in points:
            away = geo.haversine_distance_m(
                float(lat), float(lon), point["lat"], point["lon"])
            scored.append((away, point))
        scored.sort(key=lambda pair: pair[0])

        nearest_away, nearest = scored[0]
        if nearest_away > max_distance_m:
            return None

        # How many DIFFERENT houses could plausibly be the answer. The radius
        # grows with the pin's own error rather than sitting fixed: a pin
        # 0.5 m from a building has ruled its neighbours out, while a pin
        # 200 m away has ruled out nothing, and one number cannot describe
        # both.
        #
        # Counted by number and not by point, because one building can carry
        # several entrances and so several register points, and three points
        # on one house is not an ambiguous answer.
        radius = max(AMBIGUITY_FLOOR_M, AMBIGUITY_FACTOR * nearest_away)
        crowd = {
            point["cislo_domovni"]
            for away, point in scored
            if away <= radius
        }

        return {
            "cislo_popisne": nearest.get("cislo_domovni", ""),
            "cislo_orientacni": _orientacni(nearest),
            "cislo_zdroj": SOURCE_NAME,
            "cislo_vzdalenost_m": f"{nearest_away:.1f}",
            "cislo_kandidatu": str(len(crowd)),
        }, nearest


def _orientacni(point: dict) -> str:
    """"3" or "3a" - the register keeps the letter in its own column."""
    number = (point.get("cislo_orientacni") or "").strip()
    if not number:
        return ""
    return number + (point.get("znak_orientacniho") or "").strip()


def blank_match() -> dict:
    """The five fields, empty - what a listing carries when there was no
    match, so that every row has the same columns whether or not one was
    found. An empty cislo_zdroj is the marker: no number here was inferred."""
    return {field: "" for field in MATCH_FIELDS}
