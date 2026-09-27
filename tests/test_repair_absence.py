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


# --- and not what a city walk actually observed ------------------------------

def walked(on, scopes=("byt/prodej", "byt/pronajem", "dum/prodej", "dum/pronajem")):
    return (f"{on}T00:55:00+00:00", set(scopes))


def test_a_row_the_city_walk_looked_for_and_missed_stays_missing():
    """The case this tool learned the hard way it did not know about.

    Since 23 September sreality is walked city-wide every night and marks
    absence across all of Prague - legitimately. On 27 September a dry run
    offered to restore 333 listings that walk had looked for and not found,
    and the checklists said non-zero meant --apply."""
    listings = {"s1": sreality_row(last_seen_at="2026-09-25",
                                   property_type="byt", transaction_type="prodej")}
    restored, stats = repair_absence.repair(
        listings, index_in("Praha 2"), [walked("2026-09-27")])
    assert restored == 0
    assert listings["s1"]["status"] == "missing_2"
    assert stats["looked for by a city walk since last seen - genuinely absent"] == 1


def test_what_the_tool_was_written_for_is_still_repaired():
    """22 September: marked by the narrowed hourly walk, no city walk after
    it. That is still unjustified absence, and still restored."""
    listings = {"s1": sreality_row(last_seen_at="2026-09-20",
                                   property_type="byt", transaction_type="prodej")}
    restored, _ = repair_absence.repair(
        listings, index_in("Praha 2"), [walked("2026-09-19")])
    assert restored == 1
    assert listings["s1"]["status"] == "active"


def test_a_walk_that_did_not_finish_this_category_proves_nothing():
    """A walk that completed only sale says nothing about a rental's
    absence - the same per-scope rule absence marking itself follows."""
    listings = {"s1": sreality_row(last_seen_at="2026-09-25",
                                   property_type="byt", transaction_type="pronajem")}
    restored, _ = repair_absence.repair(
        listings, index_in("Praha 2"), [walked("2026-09-27", scopes=("byt/prodej",))])
    assert restored == 1


def test_main_reads_the_walks_from_the_run_log(tmp_path):
    """The wiring: repair() can know about walks and main() never tell it."""
    import json
    from common import storage
    data_dir = tmp_path / "data"; logs_dir = tmp_path / "logs"
    data_dir.mkdir(); logs_dir.mkdir()
    (logs_dir / "2026-09-27.jsonl").write_text(json.dumps({
        "started_at": "2026-09-27T00:55:00+00:00", "kind": "sreality city walk",
        "sources": {"sreality": {"run_complete": True,
                                 "scopes_absence_marked": ["byt/prodej"]}}}) + "\n",
        encoding="utf-8")
    walks = repair_absence.city_walks(logs_dir)
    assert walks == [("2026-09-27T00:55:00+00:00", {"byt/prodej"})]


def test_main_leaves_alone_what_a_logged_city_walk_covered(tmp_path, capsys):
    """End to end through main(): the walk in the run log has to reach
    repair(), or the rule above exists and is never applied."""
    import json
    from unittest.mock import patch
    from common import storage
    from common.schema import LISTING_FIELDS

    data_dir = tmp_path / "data"; logs_dir = tmp_path / "logs"
    data_dir.mkdir(); logs_dir.mkdir()
    (data_dir / "ruian_praha.csv.gz").write_bytes(b"")  # presence only; load is patched
    listing = {field: "" for field in LISTING_FIELDS}
    listing.update(sreality_row(last_seen_at="2026-09-25",
                                property_type="byt", transaction_type="prodej"))
    storage.write_listings({"s1": listing}, data_dir / "listings.csv")
    (logs_dir / "2026-09-27.jsonl").write_text(json.dumps({
        "started_at": "2026-09-27T00:55:00+00:00", "kind": "sreality city walk",
        "sources": {"sreality": {"run_complete": True,
                                 "scopes_absence_marked": ["byt/prodej"]}}}) + "\n",
        encoding="utf-8")

    with patch.object(repair_absence.ruian.Index, "load",
                      staticmethod(lambda *a, **k: index_in("Praha 2"))):
        repair_absence.main(["--data-dir", str(data_dir),
                             "--logs-dir", str(logs_dir)])

    out = capsys.readouterr().out
    assert "0 rows restored" in out, out
    assert "looked for by a city walk" in out, out
