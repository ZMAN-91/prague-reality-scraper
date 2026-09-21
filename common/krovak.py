"""S-JTSK, the coordinate system the address register uses, into WGS84.

RUIAN publishes every address point as a pair the probe printed like this:

    'Souradnice Y': '744384.54'
    'Souradnice X': '1042569.73'

Both positive, Y around 741-745 thousand and X around 1042-1046 thousand for
Prague. That is S-JTSK in its classic axis convention (EPSG:2065): X counts
southwards, Y westwards, both positive over Czechia. The same projection with
ordinary east/north axes is EPSG:5514, where the same point is (-Y, -X) - and
that negation is the whole of the conversion's sign handling. Get it wrong
and the three Prague Castle addresses below come out at 68 N 41 E, in the
Arctic Ocean north of Arkhangelsk, which is at least an obvious kind of wrong.

WHY pyproj AND NOT TWENTY LINES OF TRIGONOMETRY

The projection itself is a closed form anyone can write out. The hard half is
the datum shift: Bessel 1841 to WGS84, seven parameters that have to be exact
and whose sign convention (coordinate_frame vs position_vector) flips the
correction. Recited from memory they are plausible and wrong, and wrong by a
few metres is invisible - it does not look like a bug, it looks like a house
number one door along. pyproj carries the EPSG registry, so the numbers come
from the authority that publishes them.

PROJ offers several shifts between these two systems and does not promise
which it prefers. Measured here, its default agrees with the one-metre
transformations to 0.1 m, while the six-metre alternative sits about 10 m
away - and 10 m is the spacing of house numbers along a street, so this is
exactly the size of error that would quietly mis-assign them. The test suite
therefore pins the behaviour rather than the pipeline string: it checks the
result against an independently written seven-parameter pipeline, so a PROJ
upgrade that changed the default would fail loudly instead of shifting every
address by one door.

WHERE THIS RUNS

Only in tools/build_ruian_index.py, which runs monthly and writes the index
in WGS84. The hourly scrape reads that index with the standard library and
never imports this module, so pyproj stays out of requirements.txt and out of
the critical path.
"""

from __future__ import annotations

from typing import Optional, Tuple

# The address register's own axis convention, named so the negation below is
# a statement about RUIAN rather than a pair of unexplained minus signs.
SOURCE_CRS = "EPSG:5514"      # S-JTSK / Krovak East North
TARGET_CRS = "EPSG:4326"      # WGS84 lat/lon

# Three addresses inside Prague Castle, taken verbatim from what the register
# returned (tools/probe_ruian.py, run 2026-09-21). Used by the tests: the
# castle's position is a fact about the world, and the distances between the
# three are a fact about the register, so together they check the placement
# and the scale without either coming from this project.
CASTLE_POINTS = (
    # (name, Souradnice Y, Souradnice X)
    ("Hrad I. nadvori 1", 744384.54, 1042569.73),
    ("Jirska 2/3", 744016.88, 1042411.79),
    ("namesti U svateho Jiri 2/1", 744100.09, 1042446.40),
)

_transformer = None


def _get_transformer():
    """Built once; pyproj is imported here so that merely importing this
    module costs nothing to anything that does not convert."""
    global _transformer
    if _transformer is None:
        from pyproj import Transformer
        _transformer = Transformer.from_crs(
            SOURCE_CRS, TARGET_CRS, always_xy=True)
    return _transformer


def to_wgs84(y: float, x: float) -> Tuple[float, float]:
    """(Souradnice Y, Souradnice X) as RUIAN writes them -> (lat, lon).

    Both arguments positive, in the order the register's columns appear.
    """
    lon, lat = _get_transformer().transform(-float(y), -float(x))
    return lat, lon


def parse_point(y_text: Optional[str],
                x_text: Optional[str]) -> Optional[Tuple[float, float]]:
    """A CSV row's two coordinate cells -> (lat, lon), or None.

    Six of the register's 134,627 Prague rows have both cells empty. They are
    addresses without a surveyed point, not rows to repair, so they are
    dropped rather than defaulted - a default would put them at the origin,
    which in this projection is 59.76 N 24.83 E, just outside Tallinn.

    There is no separate emptiness check because there is nothing for one to
    do: float("") and float("None") both raise, so the guard below already
    covers empty, missing and malformed alike. An added `if not y_text`
    branch would be unreachable in every case that matters, which is worse
    than absent - it would look like the protection while the real one was
    the clause underneath it.
    """
    try:
        y = float(str(y_text).replace(",", ".").strip())
        x = float(str(x_text).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return to_wgs84(y, x)
