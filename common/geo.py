"""Geographic filtering: the Prague+okolí bounding box and the two priority zones.

Both the bounding box and the priority-zone circles below are *chosen*
values, not looked up from an authoritative source (see README "Designová
rozhodnutí" section for the reasoning). They're kept in one place, as plain
data, so they're easy to tune later without touching scraper logic.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

# --- Target area: Prague + immediate surroundings -------------------------
#
# Prague's own administrative boundary is roughly lat 49.94-50.18,
# lon 14.22-14.71. We pad that by ~0.07 deg (~7-8 km at this latitude) on
# every side so that municipalities that sit right against the Prague
# border (e.g. Jesenice, Prusiny, Zlicin surroundings, Klecany, Roztoky,
# Brandys-area villages, Prubonice, Jirny) are included, since sreality's
# and bezrealitky's own region filters key on administrative kraj boundary
# and would otherwise exclude the "tesne okoli" the brief asks for.
#
# A rectangle is a deliberately simple choice over a hand-drawn polygon: it
# is trivial to verify, trivial to tune (four numbers), and "a bit too
# generous" is the safe failure mode here (a handful of extra Stredocesky
# villages in the data cost nothing; a polygon bug that silently drops a
# chunk of real Prague listings would be much worse and much harder to
# notice years into an unattended run).
PRAGUE_BBOX = {
    "lat_min": 49.90,
    "lat_max": 50.20,
    "lon_min": 14.15,
    "lon_max": 14.75,
}


class PriorityZone(NamedTuple):
    name: str
    lat: float
    lon: float
    radius_km: float


# Resolved coordinates, not remembered ones. These sat at 50.0270/14.4780 and
# 50.0440/14.5250 for months, written from general knowledge because this
# build environment had no map service to check them against. The first is
# about 1.9 km from Spořilov - far enough that with a 1.3 km radius, a flat on
# Roztylské náměstí was NOT flagged as priority_zone, and nothing about the
# data would ever have looked wrong. Both now come from
# docs/geo/reference_points.json (see tools/probe_sources.py), and
# tests/test_collection_area.py fails if the two drift apart again.
PRIORITY_ZONES = [
    PriorityZone(name="sporilov", lat=50.04440, lon=14.47890, radius_km=1.6),
    PriorityZone(name="hostivar", lat=50.04890, lon=14.52440, radius_km=1.6),
]

EARTH_RADIUS_M = 6_371_000.0


def haversine_distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two GPS points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def is_in_target_area(lat: Optional[float], lon: Optional[float]) -> bool:
    """True if the point falls inside the Prague+okolí bounding box.

    A missing GPS coordinate is treated as *not* in the target area: we'd
    rather skip a listing we can't geographically place than silently pollute
    the dataset with something that might be in Brno.
    """
    if lat is None or lon is None:
        return False
    return (
        PRAGUE_BBOX["lat_min"] <= lat <= PRAGUE_BBOX["lat_max"]
        and PRAGUE_BBOX["lon_min"] <= lon <= PRAGUE_BBOX["lon_max"]
    )


def is_priority_zone(lat: Optional[float], lon: Optional[float]) -> bool:
    """True if the point falls within any priority-zone circle (Sporilov/Hostivar)."""
    if lat is None or lon is None:
        return False
    for zone in PRIORITY_ZONES:
        if haversine_distance_m(lat, lon, zone.lat, zone.lon) <= zone.radius_km * 1000:
            return True
    return False
