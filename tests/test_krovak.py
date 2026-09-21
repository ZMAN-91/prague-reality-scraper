"""The conversion that decides where every house number lands.

A datum shift cannot be checked by reading it. Wrong by a few metres looks
exactly like right, and the consequence - the number of the house next door -
is not detectable downstream either. So these check against things that do
not come from this project: where Prague Castle is, and how far apart the
register itself says three of its addresses are.
"""

from __future__ import annotations

import math

import pytest

from common import geo, krovak

# pyproj is deliberately not in requirements-dev.txt: the hourly scrape
# installs that file and runs this suite twenty-four times a day, and has no
# use for a projection library. So here the skip is honest - the code under
# test cannot run in that context either.
#
# It must not become a skip everywhere, which is how a test quietly stops
# existing. The index build is where this code does run, and
# test_workflows.py asserts that workflow installs requirements-ruian.txt and
# runs these tests, so exactly one place is obliged not to skip them.
pytest.importorskip(
    "pyproj",
    reason="only the monthly RUIAN index build converts coordinates")

# Prague Castle, to the nearest few hundred metres. Deliberately coarse and
# deliberately not copied from this code's own output: it only has to be
# specific enough to tell Prague from Arkhangelsk or Tallinn, which is what
# the sign and the axis order get wrong when they are wrong.
CASTLE_LAT, CASTLE_LON = 50.0900, 14.4003


def test_the_castle_addresses_land_at_the_castle():
    for name, y, x in krovak.CASTLE_POINTS:
        lat, lon = krovak.to_wgs84(y, x)
        away = geo.haversine_distance_m(lat, lon, CASTLE_LAT, CASTLE_LON)
        assert away < 500, f"{name} landed {away:.0f} m from the castle"


def test_the_axes_are_not_merely_symmetric():
    """The negation is the one place a sign can be dropped, and dropping it
    is silent: the result is still a coordinate, still a number, still on
    Earth. It is 2,000 km away, in the Arctic."""
    y, x = krovak.CASTLE_POINTS[0][1], krovak.CASTLE_POINTS[0][2]
    right = krovak.to_wgs84(y, x)

    from pyproj import Transformer
    unflipped = Transformer.from_crs(
        krovak.SOURCE_CRS, krovak.TARGET_CRS, always_xy=True)
    lon, lat = unflipped.transform(y, x)
    away = geo.haversine_distance_m(right[0], right[1], lat, lon)
    assert away > 1_000_000, (
        "dropping the negation moved the point only %.0f m; the test that "
        "the signs matter has stopped being a test" % away)


def test_distances_survive_the_projection():
    """Scale, rotation and shear in one check, and the only numbers it needs
    are the register's own.

    S-JTSK is conformal, so the distance between two points is the same in
    both systems bar the projection's scale factor - a few parts in ten
    thousand here. A transform that placed the points plausibly but with the
    wrong scale or a rotation would pass the castle check above and fail
    this one."""
    points = krovak.CASTLE_POINTS
    ratios = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            _, y1, x1 = points[i]
            _, y2, x2 = points[j]
            in_jtsk = math.hypot(y2 - y1, x2 - x1)

            lat1, lon1 = krovak.to_wgs84(y1, x1)
            lat2, lon2 = krovak.to_wgs84(y2, x2)
            in_wgs = geo.haversine_distance_m(lat1, lon1, lat2, lon2)

            assert in_jtsk > 50, "a pair too close to measure a ratio with"
            ratios.append(in_wgs / in_jtsk)

    for ratio in ratios:
        assert 0.995 < ratio < 1.005, f"scale is off by {abs(1-ratio)*100:.2f}%"
    # Uniform, not merely close: a rotation or a shear would show up as a
    # spread between pairs even when each ratio is individually near 1.
    assert max(ratios) - min(ratios) < 0.002, f"ratios disagree: {ratios}"


def test_the_default_pipeline_still_matches_a_seven_parameter_shift():
    """PROJ chooses between several published S-JTSK-to-WGS84 shifts and does
    not promise which. The one it currently picks agrees with the accurate
    ones to 0.1 m; a six-metre alternative it also offers sits about 10 m
    away, which is the spacing of house numbers along a street.

    So this pins the behaviour, not the choice: an upgrade that changed the
    default would move every address by one door, and would fail here."""
    from pyproj import Transformer
    seven_param = Transformer.from_pipeline(
        "+proj=pipeline "
        "+step +inv +proj=krovak +lat_0=49.5 +lon_0=24.8333333333333 "
        "+alpha=30.2881397527778 +k=0.9999 +x_0=0 +y_0=0 +ellps=bessel "
        "+step +proj=push +v_3 "
        "+step +proj=cart +ellps=bessel "
        "+step +proj=helmert +x=572.213 +y=85.334 +z=461.94 "
        "+rx=-4.9732 +ry=-1.529 +rz=-5.2484 +s=3.5378 "
        "+convention=coordinate_frame "
        "+step +inv +proj=cart +ellps=WGS84 "
        "+step +proj=pop +v_3 "
        "+step +proj=unitconvert +xy_in=rad +xy_out=deg")

    for name, y, x in krovak.CASTLE_POINTS:
        lat, lon = krovak.to_wgs84(y, x)
        lon_ref, lat_ref = seven_param.transform(-y, -x)
        away = geo.haversine_distance_m(lat, lon, lat_ref, lon_ref)
        assert away < 1.0, (
            f"{name}: PROJ's default is {away:.2f} m from the seven-parameter "
            "shift; it has probably changed which transformation it prefers")


def test_a_row_without_a_surveyed_point_is_dropped_not_defaulted():
    """Six of the register's 134,627 Prague rows have both cells empty.
    Defaulted to zero they would all land on one spot outside Tallinn and
    then be matched to whichever listing was nearest to nothing."""
    assert krovak.parse_point("", "") is None
    assert krovak.parse_point(None, None) is None
    assert krovak.parse_point("744384.54", "") is None
    assert krovak.parse_point("", "1042569.73") is None
    assert krovak.parse_point("not a number", "1042569.73") is None


def test_a_row_with_a_point_is_parsed_as_the_register_writes_it():
    lat, lon = krovak.parse_point("744384.54", "1042569.73")
    direct = krovak.to_wgs84(744384.54, 1042569.73)
    assert (lat, lon) == direct
    assert geo.haversine_distance_m(lat, lon, CASTLE_LAT, CASTLE_LON) < 500
