"""Matching a listing to an address point, and being honest about it.

The register is not available to these tests, so the index here is built by
hand - but built to real geometry: points 15 m apart along a street, which is
what Prague terraced housing actually looks like, because the whole question
is whether a pin can tell neighbours apart at that spacing.
"""

from __future__ import annotations

import csv
import gzip
import math

import pytest

from common import geo, ruian

# A point in Chodov, and a metre expressed in degrees there, so the fixtures
# below can be written in metres and read as geography.
BASE_LAT, BASE_LON = 50.0300, 14.5100
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(BASE_LAT))


def at(north_m: float, east_m: float):
    return (BASE_LAT + north_m / M_PER_DEG_LAT,
            BASE_LON + east_m / M_PER_DEG_LON)


_next_adm = iter(range(21_000_000, 22_000_000))


def point(street: str, cislo: str, north_m: float, east_m: float,
          orientacni: str = "", znak: str = "", typ: str = "č.p."):
    lat, lon = at(north_m, east_m)
    return {
        # Unique per POINT, as the register's own key is. Reusing one id for
        # every entrance of a house would make counting by point and counting
        # by house indistinguishable, and the difference between them is what
        # test_several_entrances_to_one_house_are_one_candidate exists for.
        "kod_adm": str(next(_next_adm)),
        "ulice": street,
        "ulice_original": street.title(),
        "cislo_domovni": cislo,
        "typ_cisla": typ,
        "cislo_orientacni": orientacni,
        "znak_orientacniho": znak,
        "mestska_cast": "Praha 11",
        "obvod": "Praha 4",
        "cast_obce": "Chodov",
        "psc": "14900",
        "lat": lat,
        "lon": lon,
    }


def index_of(*points) -> ruian.Index:
    by_street = {}
    for entry in points:
        by_street.setdefault(entry["ulice"], []).append(entry)
    return ruian.Index(by_street)


# A street of five houses, 15 m apart, numbered as Czech house numbers
# actually are: not in order along the street.
STREET = index_of(
    point("leopoldova", "1204", 0, 0, orientacni="2"),
    point("leopoldova", "87", 0, 15, orientacni="4"),
    point("leopoldova", "2310", 0, 30, orientacni="6"),
    point("leopoldova", "915", 0, 45, orientacni="8"),
    point("leopoldova", "33", 0, 60, orientacni="10"),
)


def test_a_pin_on_the_building_gets_that_building():
    lat, lon = at(0, 30)
    got = STREET.match(lat, lon, "Leopoldova")
    assert got["cislo_popisne"] == "2310"
    assert got["cislo_orientacni"] == "6"
    assert got["cislo_zdroj"] == "ruian"
    assert float(got["cislo_vzdalenost_m"]) < 1.0


def test_the_distance_is_recorded_not_rounded_away():
    """The distance is the only thing that says how much to believe the
    number, so it has to survive as a number."""
    # 40 m at right angles to the street, off the back of number 2310.
    # Along the street would only have landed next to a different house.
    lat, lon = at(40, 30)
    got = STREET.match(lat, lon, "Leopoldova")
    assert got["cislo_popisne"] == "2310", got
    assert 35 < float(got["cislo_vzdalenost_m"]) < 45, got


def test_a_pin_between_two_houses_says_so():
    """Halfway between neighbours the answer is a coin toss, and the point of
    cislo_kandidatu is that the row admits it rather than reading like the
    confident case."""
    lat, lon = at(0, 7.5)              # midway between 1204 and 87
    got = STREET.match(lat, lon, "Leopoldova")
    assert got["cislo_popisne"] in {"1204", "87"}
    assert int(got["cislo_kandidatu"]) >= 2, got


def test_a_pin_on_the_building_is_not_called_ambiguous():
    """A pin that lands on a building has ruled its neighbours out, even in a
    terraced street where they are 15 m away. Measured over 5,652 real
    matches the portals' pins are 3.1 m out at the median and a quarter land
    within 0.1 m, so this is the common case, not the lucky one."""
    lat, lon = at(0, 0)
    got = STREET.match(lat, lon, "Leopoldova")
    assert got["cislo_popisne"] == "1204"
    assert int(got["cislo_kandidatu"]) == 1, got


def test_a_vague_pin_is_still_called_ambiguous():
    """The counterpart: the radius has to grow with the error, or the same
    change that stops crying wolf also stops warning at all."""
    # 30 m off the street, behind the middle house. Every house on the row
    # is then between 30 and 42 m away, and the pin distinguishes none of
    # them.
    got = STREET.match(*at(30, 30), "Leopoldova")
    assert float(got["cislo_vzdalenost_m"]) > 25
    assert int(got["cislo_kandidatu"]) >= 4, got


def test_several_entrances_to_one_house_are_one_candidate():
    """A block of flats carries one cislo popisne and several register
    points. Counting points would make every such address look ambiguous."""
    block = index_of(
        point("kupeckeho", "576", 0, 0, orientacni="1"),
        point("kupeckeho", "576", 0, 8, orientacni="3"),
        point("kupeckeho", "576", 0, 16, orientacni="5"),
    )
    got = block.match(*at(0, 8), "Kupeckeho")
    assert got["cislo_popisne"] == "576"
    assert got["cislo_kandidatu"] == "1", got


def test_the_ambiguity_radius_follows_the_pin_not_a_fixed_circle():
    """A pin 200 m off the street is guessing among everything near it. With
    a radius fixed at the pin it would find one house inside 25 m, none at
    all, and report a single confident candidate."""
    lat, lon = at(200, 30)
    got = STREET.match(lat, lon, "Leopoldova")
    assert got is not None
    assert float(got["cislo_vzdalenost_m"]) > 190
    assert int(got["cislo_kandidatu"]) >= 3, (
        "a pin this far out cannot distinguish neighbours: %s" % got)


def test_diacritics_do_not_have_to_agree():
    got = index_of(point("rohacova", "18", 0, 0)).match(
        *at(0, 0), "Roháčova")
    assert got["cislo_popisne"] == "18"


def test_case_does_not_have_to_agree():
    got = index_of(point("rohacova", "18", 0, 0)).match(
        *at(0, 0), "ROHACOVA")
    assert got["cislo_popisne"] == "18"


def test_a_street_the_register_does_not_know_is_a_miss_not_a_guess():
    assert STREET.match(*at(0, 0), "Neexistujici") is None


def test_no_coordinate_is_a_miss():
    assert STREET.match(None, None, "Leopoldova") is None
    assert STREET.match(BASE_LAT, None, "Leopoldova") is None


def test_a_missing_street_is_not_stringified_into_a_lookup():
    """Without the guard, str(None).lower() is "none" - a perfectly good
    dictionary key. Prague has no street that folds to it today, so the bug
    would sit there until one appeared and then hand its house numbers to
    every listing whose street the portal left blank."""
    odd = index_of(point("none", "1", 0, 0))
    assert odd.match(*at(0, 0), None) is None
    assert odd.match(*at(0, 0), "") is None


def test_no_street_is_a_miss():
    """Half the listings have a coordinate and no street. Matching them to
    the nearest point of ANY street is exactly the wrong thing to do - it
    would always find something, and always be a guess."""
    assert STREET.match(*at(0, 0), "") is None
    assert STREET.match(*at(0, 0), None) is None


def test_a_point_too_far_away_is_refused():
    lat, lon = at(0, 5000)
    assert STREET.match(lat, lon, "Leopoldova") is None


def test_the_distance_bound_is_the_caller_s_to_tighten():
    lat, lon = at(0, 30 + 100)
    assert STREET.match(lat, lon, "Leopoldova") is not None
    assert STREET.match(lat, lon, "Leopoldova", max_distance_m=50.0) is None


def test_the_orientacni_letter_is_joined_to_its_number():
    got = index_of(point("bryksova", "944", 0, 0,
                         orientacni="9", znak="a")).match(
        *at(0, 0), "Bryksova")
    assert got["cislo_orientacni"] == "9a"


def test_a_missing_orientacni_is_empty_not_a_stray_letter():
    got = index_of(point("bryksova", "944", 0, 0,
                         orientacni="", znak="a")).match(
        *at(0, 0), "Bryksova")
    assert got["cislo_orientacni"] == ""


def test_every_match_carries_every_field():
    got = STREET.match(*at(0, 0), "Leopoldova")
    assert set(got) == set(ruian.MATCH_FIELDS)
    assert set(ruian.blank_match()) == set(ruian.MATCH_FIELDS)
    assert all(value == "" for value in ruian.blank_match().values())


def test_an_index_round_trips_through_the_file_it_is_written_as(tmp_path):
    """The builder writes gzipped CSV with float coordinates as text; the
    loader has to turn them back into numbers or every distance is a
    TypeError at the first haversine."""
    path = tmp_path / "ruian.csv.gz"
    from tools import build_ruian_index

    rows = []
    for entry in STREET.by_street["leopoldova"]:
        row = dict(entry)
        row["lat"] = f"{entry['lat']:.6f}"
        row["lon"] = f"{entry['lon']:.6f}"
        rows.append(row)
    build_ruian_index.write_index(rows, str(path))

    loaded = ruian.Index.load(str(path))
    assert len(loaded) == 5
    assert loaded.streets == 1
    got = loaded.match(*at(0, 30), "Leopoldova")
    assert got["cislo_popisne"] == "2310"
    assert float(got["cislo_vzdalenost_m"]) < 1.0


def test_a_row_the_builder_could_not_place_is_skipped_on_load(tmp_path):
    path = tmp_path / "ruian.csv.gz"
    with gzip.open(path, "wt", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ulice", "cislo_domovni",
                                                    "lat", "lon"])
        writer.writeheader()
        writer.writerow({"ulice": "leopoldova", "cislo_domovni": "1",
                         "lat": "50.03", "lon": "14.51"})
        writer.writerow({"ulice": "leopoldova", "cislo_domovni": "2",
                         "lat": "", "lon": ""})
        writer.writerow({"ulice": "", "cislo_domovni": "3",
                         "lat": "50.03", "lon": "14.51"})
    loaded = ruian.Index.load(str(path))
    assert len(loaded) == 1


def test_a_pin_dead_on_a_point_does_not_claim_infinite_precision():
    """Two houses sharing a wall carry two numbers and two register points a
    couple of metres apart. A pin landing 20 cm from one of them is not
    evidence it is that one rather than its neighbour - GPS does not resolve
    20 cm, and a radius that scaled purely with the error would shrink to
    nothing here and report a single confident candidate."""
    pair = index_of(point("belohorska", "17", 0, 0),
                    point("belohorska", "19", 0, 2))
    got = pair.match(*at(0, 0.2), "Belohorska")
    assert float(got["cislo_vzdalenost_m"]) < 1
    assert int(got["cislo_kandidatu"]) == 2, got


def test_rebuilding_an_unchanged_index_produces_an_identical_file(tmp_path):
    """The index is 2.5 MB and lives in a git repository. gzip.open stamps
    the current time into the header, so a monthly rebuild committed a fresh
    copy every time while the backfill reported "0 rows would change" - which
    is precisely the waste the day-granular last_seen_at exists to avoid.

    The register really is unchanged most months, so identical content has to
    mean an identical file."""
    import time
    from tools import build_ruian_index

    rows = []
    for entry in STREET.by_street["leopoldova"]:
        row = dict(entry)
        row["lat"] = f"{entry['lat']:.6f}"
        row["lon"] = f"{entry['lon']:.6f}"
        rows.append(row)

    first = tmp_path / "a.csv.gz"
    build_ruian_index.write_index(rows, str(first))
    time.sleep(1.1)                      # a different second, on the clock
    second = tmp_path / "b.csv.gz"
    build_ruian_index.write_index(rows, str(second))

    assert first.read_bytes() == second.read_bytes(), (
        "two builds of the same data produced different bytes, so every "
        "monthly rebuild commits a new 2.5 MB blob")


def test_a_changed_index_does_produce_a_different_file(tmp_path):
    """The counterpart: byte-stability must not come from writing a constant."""
    from tools import build_ruian_index

    def as_text(entries):
        out = []
        for entry in entries:
            row = dict(entry)
            row["lat"] = f"{entry['lat']:.6f}"
            row["lon"] = f"{entry['lon']:.6f}"
            out.append(row)
        return out

    base = STREET.by_street["leopoldova"]
    first = tmp_path / "a.csv.gz"
    second = tmp_path / "b.csv.gz"
    build_ruian_index.write_index(as_text(base), str(first))
    build_ruian_index.write_index(as_text(base[:-1]), str(second))
    assert first.read_bytes() != second.read_bytes()


def test_the_numbering_series_travels_with_the_number():
    """2.81% of Prague's address points are a cislo evidencni, a separate
    series: evidencni 163 on a street is not house 163 on that street. 18 of
    5,652 real matches land on one, which is small enough never to be noticed
    and systematic enough to always be wrong."""
    got = STREET.match(*at(0, 30), "Leopoldova")
    assert got["cislo_typ"] == "č.p."

    chata = index_of(point("uzakrutu", "163", 0, 0, typ="č.ev."))
    got = chata.match(*at(0, 0), "Uzakrutu")
    assert got["cislo_popisne"] == "163"
    assert got["cislo_typ"] == "č.ev.", got


def test_the_nearest_point_wins_even_when_it_is_the_other_series():
    """Reaching past it for a cislo popisne would substitute a different
    building for the right one - worse than a number that says what it is."""
    mixed = index_of(point("uzakrutu", "163", 0, 0, typ="č.ev."),
                     point("uzakrutu", "8", 0, 60, typ="č.p."))
    got = mixed.match(*at(0, 2), "Uzakrutu")
    assert got["cislo_popisne"] == "163"
    assert got["cislo_typ"] == "č.ev."
