"""What sreality is and is not collected for.

The point of these tests is that the area is *checkable*: every place the
brief named must be inside, obviously unrelated parts of Prague must be
outside, and the constants must still match the coordinates they were
resolved from. That last check exists because the failure it guards against
is invisible - a centre 2 km out collects listings happily, just the wrong
ones.
"""

import json
from pathlib import Path

import pytest

from common import collection_area as area
from common.collection_area import Capsule, Circle, contains, distance_to_segment_m

REFERENCE_FILE = Path(__file__).resolve().parent.parent / "docs" / "geo" / "reference_points.json"


# --- the places the brief asked for ---------------------------------------


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("Sporilov", 50.04440, 14.47890),
        ("Roztylske namesti", 50.04588, 14.47715),
        ("Hostivar", 50.04890, 14.52440),
        ("Horni Mecholupy", 50.04530, 14.55750),
        ("Lesopark Hostivar", 50.04383, 14.52901),
        ("Hostivarska prehrada", 50.04000, 14.53889),
        ("Nurmiho (a real collected listing)", 50.04525, 14.52430),
    ],
)
def test_every_named_place_is_collected(name, lat, lon):
    assert contains(lat, lon) is True, f"{name} must be inside the collected area"


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        # Named in the brief only as "areas that lie along the belt", but they
        # do lie along it, so they must come along for the ride.
        ("Zabehlice", 50.05694, 14.49944),
        ("Zahradni Mesto", 50.05720, 14.50140),
        ("Dolni Mecholupy", 50.05900, 14.55800),
        ("Petrovice", 50.03667, 14.56222),
        ("Chodov", 50.03139, 14.49167),
    ],
)
def test_the_belt_picks_up_what_lies_between(name, lat, lon):
    assert contains(lat, lon) is True


@pytest.mark.parametrize(
    "name,lat,lon",
    [
        ("Prague centre", 50.0875, 14.4213),
        ("Modrany", 50.0088, 14.4102),
        ("Liben", 50.11863, 14.45669),
        ("Stodulky", 50.036457, 14.338186),
        ("Dejvice", 50.11391, 14.38833),
        ("Brno", 49.1951, 16.6068),
    ],
)
def test_the_rest_of_the_country_is_not_collected(name, lat, lon):
    assert contains(lat, lon) is False


def test_a_point_without_coordinates_is_never_collected():
    assert contains(None, None) is False
    assert contains(50.045, None) is False


# --- the belt behaves like a belt -----------------------------------------


def test_the_belt_joins_its_two_ends_rather_than_being_two_islands():
    """A point halfway between Sporilov and Horni Mecholupy, on no named
    place, is still collected - that is what "pas obklopujici spojnici"
    means."""
    midpoint_lat, midpoint_lon = 50.0449, 14.5182
    assert contains(midpoint_lat, midpoint_lon) is True


def test_the_belt_stops_at_its_ends_instead_of_running_across_the_city():
    """Measured to the segment, not to an infinite line: 8 km due east of
    Horni Mecholupy is not 'near the line joining Sporilov to it'."""
    assert contains(50.0453, 14.6700) is False


def test_distance_to_segment_is_measured_to_the_nearer_end_when_past_it():
    beyond = distance_to_segment_m(50.04, 14.60, 50.04, 14.50, 50.04, 14.54)
    to_end = distance_to_segment_m(50.04, 14.60, 50.04, 14.54, 50.04, 14.54)
    assert round(beyond) == round(to_end)


def test_a_zero_length_belt_degrades_to_a_circle():
    """Guard for the divide-by-zero that a copy-paste of the same endpoint
    twice would otherwise cause."""
    degenerate = Capsule("x", 50.04, 14.52, 50.04, 14.52, half_width_km=1.0)
    assert degenerate.contains(50.045, 14.52) is True
    assert degenerate.contains(50.09, 14.52) is False


def test_circle_edge_is_inclusive_within_a_metre():
    circle = Circle("x", 50.0, 14.5, radius_km=1.0)
    assert circle.contains(50.0, 14.5) is True
    assert circle.contains(50.0 + 1.0 / 111.32, 14.5) is True   # ~1000 m north
    assert circle.contains(50.0 + 1.2 / 111.32, 14.5) is False  # ~1200 m north


# --- the constants must match where they came from ------------------------


def test_every_area_centre_matches_the_resolved_reference_point():
    """The check that makes the coordinates trustworthy rather than merely
    present. A centre that drifts from its source fails here instead of
    silently collecting the wrong square kilometre - which is precisely what
    the previous hand-written Sporilov constant did for months."""
    reference = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))["places"]
    by_name = {}
    for query, point in reference.items():
        # "Sporilov, Praha, Czechia" -> "sporilov"
        by_name[query.split(",")[0].strip().lower().replace(" ", "-")] = point

    checked = 0
    for circle in area.PLACES:
        point = by_name.get(circle.name)
        if point is None:
            continue  # named in the code but not among the resolved queries
        assert abs(circle.lat - point["lat"]) < 1e-4, f"{circle.name} latitude drifted"
        assert abs(circle.lon - point["lon"]) < 1e-4, f"{circle.name} longitude drifted"
        checked += 1
    assert checked >= 6, "too few centres cross-checked for this test to mean anything"


def test_the_belt_ends_are_sporilov_and_horni_mecholupy():
    reference = json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))["places"]
    sporilov = reference["Sporilov, Praha, Czechia"]
    mecholupy = reference["Horni Mecholupy, Praha, Czechia"]
    belt = area.SPORILOV_TO_MECHOLUPY
    assert (round(belt.lat_a, 4), round(belt.lon_a, 4)) == (round(sporilov["lat"], 4), round(sporilov["lon"], 4))
    assert (round(belt.lat_b, 4), round(belt.lon_b, 4)) == (round(mecholupy["lat"], 4), round(mecholupy["lon"], 4))


# --- the cheap name pre-filter --------------------------------------------


def test_name_hint_only_decides_whether_a_lookup_is_worth_one_request():
    assert area.name_suggests_area("Nurmiho, Praha 15 Hostivar, Praha") is True
    assert area.name_suggests_area("Roztylske Namesti, Praha 4 Sporilov, Praha") is True
    assert area.name_suggests_area("Ceskoslovenskeho Exilu, Modrany, Praha") is False
    assert area.name_suggests_area(None) is False


def test_describe_is_loggable():
    summary = area.describe()
    json.dumps(summary)
    assert summary["belt_km"] == pytest.approx(5.6, abs=0.2)
    assert "roztylske-namesti" in summary["places"]
