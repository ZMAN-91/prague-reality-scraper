"""What sreality is collected *for*: a defined patch of south-east Prague.

Until now sreality was swept city-wide and every Prague listing was stored.
That is no longer what this project is for. The area below is the one asked
for: Spořilov (with Roztylské náměstí at its heart), Hostivař, Horní
Měcholupy, the north side of the Hostivařský lesopark where it meets those
two, and - the part that ties them together - a belt along the line joining
Spořilov to Horní Měcholupy, everything inside which is kept.

A listing outside this area is not stored at all. bezrealitky is unaffected
and still covers the whole city;
this is a sreality-only restriction, because sreality is the source where
collecting less is also the point (docs/podminky.md).

## Why circles and a capsule, not a polygon

A polygon of the actual cadastral boundaries would be more precise and far
worse to live with: nobody can check it by reading it, a single transposed
digit silently drops a neighbourhood, and adding "the bit I forgot" means
re-drawing it. Two primitives cover the brief exactly:

  - a **circle** for each named place, radius chosen to cover its built-up
    area;
  - a **capsule** - every point within a given distance of a line segment -
    for the belt between Spořilov and Horní Měcholupy.

Both are four numbers each, both are obvious when read aloud, and the union
of them is easy to extend: the brief explicitly says areas may have been
forgotten, so adding one must be a one-line change. It is.

## Where the coordinates come from

Every centre below was resolved against a real service and recorded, with
the query sent and the name that came back, in `docs/geo/reference_points.json`
(see tools/probe_sources.py). They are repeated here as constants so the
scraper never depends on that file at runtime, and tests/test_collection_area.py
asserts the two agree - so if a future re-resolution moves a point, a test
fails instead of the scraper quietly watching a different square kilometre.
That check exists because an earlier hand-written "Spořilov" constant was
about 1.5 km off, in a way nothing in the data would have revealed.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from typing import Optional

from common.geo import EARTH_RADIUS_M, haversine_distance_m


@dataclass(frozen=True)
class Circle:
    """A named place and how far around its centre counts as that place."""

    name: str
    lat: float
    lon: float
    radius_km: float
    note: str = ""

    def contains(self, lat: float, lon: float) -> bool:
        return haversine_distance_m(lat, lon, self.lat, self.lon) <= self.radius_km * 1000


@dataclass(frozen=True)
class Capsule:
    """Everything within `half_width_km` of the segment between two points.

    This is the "pás obklopující spojnici" - a belt around the line joining
    two places - and it is the shape that makes the area one connected
    region instead of separate islands.
    """

    name: str
    lat_a: float
    lon_a: float
    lat_b: float
    lon_b: float
    half_width_km: float
    note: str = ""

    def contains(self, lat: float, lon: float) -> bool:
        return distance_to_segment_m(
            lat, lon, self.lat_a, self.lon_a, self.lat_b, self.lon_b
        ) <= self.half_width_km * 1000


def _to_local_xy(lat: float, lon: float, lat_ref: float) -> tuple[float, float]:
    """Metres east/north of (0, 0), with longitude shrunk by the latitude.

    An equirectangular projection is wrong over long distances and exactly
    right here: the whole area is about 8 km across, where the error is
    centimetres, and it turns "distance from a point to a line on a sphere"
    into ordinary plane geometry that can be read and checked.
    """
    x = math.radians(lon) * math.cos(math.radians(lat_ref)) * EARTH_RADIUS_M
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def distance_to_segment_m(
    lat: float, lon: float, lat_a: float, lon_a: float, lat_b: float, lon_b: float
) -> float:
    """Shortest distance in metres from a point to the segment AB.

    Note segment, not infinite line: a point far beyond either end is
    measured to that end, which is what keeps the belt from stretching
    across the whole city.
    """
    lat_ref = (lat_a + lat_b) / 2
    px, py = _to_local_xy(lat, lon, lat_ref)
    ax, ay = _to_local_xy(lat_a, lon_a, lat_ref)
    bx, by = _to_local_xy(lat_b, lon_b, lat_ref)

    abx, aby = bx - ax, by - ay
    segment_length_sq = abx * abx + aby * aby
    if segment_length_sq == 0:  # A and B are the same point; fall back to a circle
        return math.hypot(px - ax, py - ay)

    # How far along AB the closest point lies, clamped to the segment itself.
    t = ((px - ax) * abx + (py - ay) * aby) / segment_length_sq
    t = max(0.0, min(1.0, t))
    closest_x, closest_y = ax + t * abx, ay + t * aby
    return math.hypot(px - closest_x, py - closest_y)


# --- The area itself ------------------------------------------------------
#
# Every lat/lon below appears in docs/geo/reference_points.json with the query
# that produced it and the name the service matched, and
# tests/test_collection_area.py checks the two still agree.

# The belt. "Pás obklopující spojnici Spořilova a Horních Měcholup" - and it
# is the shape that does the real work, because it turns a handful of separate
# neighbourhoods into one connected region and automatically picks up whatever
# lies between them. 1.8 km either side is a deliberate over-reach: the brief
# says outright that some areas may not have been named, and a belt that is
# slightly too wide costs a few extra listings, while one that is too narrow
# silently omits streets nobody notices are missing.
SPORILOV_TO_MECHOLUPY = Capsule(
    name="pas-sporilov-horni-mecholupy",
    lat_a=50.04440, lon_a=14.47890,   # Spořilov
    lat_b=50.04530, lon_b=14.55750,   # Horní Měcholupy
    half_width_km=1.8,
    note="the belt joining the two ends of the watched area; ~5.6 km long",
)

# Named places. The first four are the ones the brief calls out; the rest lie
# on or beside the belt and are named explicitly so that what is collected can
# be read off this list rather than inferred from geometry.
PLACES = [
    Circle("sporilov", 50.04440, 14.47890, 1.6, "west end of the belt"),
    Circle("roztylske-namesti", 50.04588, 14.47715, 0.8,
           "singled out in the brief; sits inside Spořilov and is named anyway"),
    Circle("hostivar", 50.04890, 14.52440, 1.6, "middle of the belt"),
    Circle("horni-mecholupy", 50.04530, 14.55750, 1.6, "east end of the belt"),
    Circle("hostivarsky-lesopark", 50.04383, 14.52901, 1.6,
           "the park, and with this radius its whole northern edge where it "
           "meets Hostivař and Horní Měcholupy - the side the brief asks for"),
    Circle("hostivarska-prehrada", 50.04000, 14.53889, 1.2, "south-east of the park"),
    Circle("dolni-mecholupy", 50.05900, 14.55800, 1.2, "directly north of Horní Měcholupy"),
    Circle("zabehlice", 50.05694, 14.49944, 1.3, "north side of the belt"),
    Circle("zahradni-mesto", 50.05720, 14.50140, 1.3, "north side of the belt"),
    Circle("petrovice", 50.03667, 14.56222, 1.2, "south-east, past the belt's east end"),
    Circle("chodov", 50.03139, 14.49167, 1.3, "south side of the belt"),
]

SHAPES = [SPORILOV_TO_MECHOLUPY, *PLACES]

# City-part and street names that appear in a sreality index row even when it
# carries no GPS. Used only in that case, and only to decide whether one
# detail request is worth spending to find out where the listing really is -
# never to admit a listing. Geography is always decided by coordinates.
NAME_HINTS = (
    "sporilov", "roztyl", "hostivar", "mecholup", "zabehlic",
    "zahradni-mesto", "zahradni mesto", "petrovic", "chodov",
    "nurmiho", "milicov", "kozmikova", "hornomecholupska",
)


def contains(lat: Optional[float], lon: Optional[float]) -> bool:
    """True if the point is inside the collected area.

    A missing coordinate is False, the same rule common.geo uses: an unplaced
    listing is not admitted on the strength of a hopeful guess.
    """
    if lat is None or lon is None:
        return False
    return any(shape.contains(lat, lon) for shape in SHAPES)


def shape_containing(lat: Optional[float], lon: Optional[float]) -> Optional[str]:
    """Name of the first shape containing the point, for logs and tests."""
    if lat is None or lon is None:
        return None
    for shape in SHAPES:
        if shape.contains(lat, lon):
            return shape.name
    return None


def name_suggests_area(text: Optional[str]) -> bool:
    """Cheap pre-filter for an index row that has no coordinates.

    sreality index rows carry the city part and street even when GPS is
    missing, so this answers "is this worth one request to place properly?"
    without spending that request on all of Prague. It decides nothing about
    whether a listing is kept.
    """
    if not text:
        return False
    return any(hint in _fold(text) for hint in NAME_HINTS)


def _fold(text: str) -> str:
    """Lower-cased and stripped of diacritics.

    Necessary because the sources disagree: sreality's index gives SEO slugs
    ("praha-4-sporilov"), while iDNES prints the real thing ("Praha 10 -
    Hostivar" with the hacek). Comparing them without folding silently
    matches one source and not the other.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def describe() -> dict:
    """Loggable summary of what is being collected."""
    return {
        "shapes": len(SHAPES),
        "belt_km": round(
            haversine_distance_m(
                SPORILOV_TO_MECHOLUPY.lat_a, SPORILOV_TO_MECHOLUPY.lon_a,
                SPORILOV_TO_MECHOLUPY.lat_b, SPORILOV_TO_MECHOLUPY.lon_b,
            ) / 1000, 2),
        "belt_half_width_km": SPORILOV_TO_MECHOLUPY.half_width_km,
        "places": [shape.name for shape in PLACES],
    }
