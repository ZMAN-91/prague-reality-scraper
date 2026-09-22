"""Undoing absence a walk was never entitled to declare.

`missing_N` means "this walk covered the population and did not find it".
For one day the narrowed sreality walk meant "I looked at two districts out
of the city" and said it anyway.
"""

from __future__ import annotations

from tests.test_ruian import at, index_of, point
from tools import repair_absence


def sreality_row(status="missing_2", north=0, east=0, ulice="Leopoldova",
                 **extra):
    lat, lon = at(north, east)
    row = {"internal_id": "s1", "source": "sreality", "status": status,
           "lat": str(lat), "lon": str(lon), "ulice": ulice,
           "last_seen_at": "2026-09-20"}
    row.update(extra)
    return row


def index_in(obvod):
    entry = point("leopoldova", "1", 0, 0)
    entry["obvod"] = obvod
    return index_of(entry)


def test_a_row_outside_the_walked_districts_is_restored():
    rows = {"s1": sreality_row()}
    restored, _ = repair_absence.repair(rows, index_in("Praha 5"))
    assert restored == 1
    assert rows["s1"]["status"] == "active"


def test_a_row_inside_the_walked_districts_stays_missing():
    """It was genuinely looked for. Resetting it would erase a real
    disappearance to tidy up a different mistake."""
    for obvod in ("Praha 4", "Praha 10"):
        rows = {"s1": sreality_row()}
        restored, _ = repair_absence.repair(rows, index_in(obvod))
        assert restored == 0, obvod
        assert rows["s1"]["status"] == "missing_2"


def test_the_last_seen_date_is_never_rewritten():
    """It says Sunday, that is true, and it is now the only honest signal
    about these rows."""
    rows = {"s1": sreality_row()}
    repair_absence.repair(rows, index_in("Praha 5"))
    assert rows["s1"]["last_seen_at"] == "2026-09-20"


def test_only_sreality_is_touched():
    """iDNES carries its own guard and bezrealitky is always city-wide, so
    absence marked for either was entitled."""
    for source in ("idnes", "bezrealitky"):
        rows = {"s1": sreality_row(source=source)}
        restored, _ = repair_absence.repair(rows, index_in("Praha 5"))
        assert restored == 0, source


def test_an_active_row_is_left_alone():
    rows = {"s1": sreality_row(status="active")}
    restored, _ = repair_absence.repair(rows, index_in("Praha 5"))
    assert restored == 0
    assert rows["s1"]["status"] == "active"


def test_a_row_without_a_coordinate_is_left_alone():
    """There is no way to tell whether the walk covered it, and guessing
    either way is worse than leaving it exactly as it is."""
    rows = {"s1": sreality_row()}
    rows["s1"]["lat"] = ""
    restored, stats = repair_absence.repair(rows, index_in("Praha 5"))
    assert restored == 0
    assert stats["no coordinate - left alone"] == 1


def test_a_row_the_register_cannot_place_is_left_alone():
    rows = {"s1": sreality_row(ulice="Neexistujici")}
    restored, stats = repair_absence.repair(rows, index_in("Praha 5"))
    assert restored == 0
    assert stats["not in the register - left alone"] == 1
