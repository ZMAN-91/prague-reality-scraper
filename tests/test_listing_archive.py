"""listings.csv holds the live rows; removed ones live in monthly archives.

What matters is that nothing outside storage can tell: the union reads back
exactly what was written, every row sits in exactly one file, a listing that
comes back from the archive moves back without a copy left behind, and no
write - however it is interrupted - loses a row.
"""

import csv
import itertools

import pytest

from common import storage
from common.schema import LISTING_FIELDS


def row(n, status="active", last_seen="2026-09-20", **extra):
    base = {f: "" for f in LISTING_FIELDS}
    base.update(internal_id=f"sreality:{n}", source="sreality", source_id=str(n),
                status=status, first_seen_at="2026-09-01T10:00:00+00:00",
                last_seen_at=last_seen)
    base.update(extra)
    return base


def ids_in(path):
    if not path.exists():
        return None
    with open(path, newline="", encoding="utf-8") as f:
        return [r["internal_id"] for r in csv.DictReader(f)]


def everywhere(path):
    """internal_id -> the files it is in."""
    seen = {}
    for p in storage.listing_files(path):
        for i in ids_in(p):
            seen.setdefault(i, []).append(p.name)
    return seen


@pytest.fixture
def path(tmp_path):
    return tmp_path / "data" / "listings.csv"


def test_removed_rows_go_to_the_month_they_were_last_seen(path):
    rows = {r["internal_id"]: r for r in [
        row(1), row(2, "missing_2"),
        row(3, "removed", "2026-08-30"), row(4, "removed", "2026-09-02T00:00:00+00:00"),
    ]}
    storage.write_listings(rows, path)

    assert ids_in(path) == ["sreality:1", "sreality:2"]
    archive = path.parent / "listings-archive"
    assert ids_in(archive / "2026-08.csv") == ["sreality:3"]
    assert ids_in(archive / "2026-09.csv") == ["sreality:4"]
    assert storage.read_listings(path) == rows


@pytest.mark.parametrize("last_seen", ["", "zitra", "2026-9-01", "20260901"])
def test_a_removed_row_without_a_usable_date_stays_live(path, last_seen):
    """Filed under a name that is not a month, it would never be read back."""
    storage.write_listings({"sreality:1": row(1, "removed", last_seen)}, path)
    assert ids_in(path) == ["sreality:1"]
    assert list(storage.read_listings(path)) == ["sreality:1"]


def test_reappearing_from_the_archive_moves_it_back(path):
    storage.write_listings({"sreality:1": row(1), "sreality:2": row(2, "removed", "2025-11-03")}, path)
    listings = storage.read_listings(path)
    listings["sreality:2"].update(status="active", last_seen_at="2027-01-05")
    storage.write_listings(listings, path)

    assert ids_in(path) == ["sreality:1", "sreality:2"]
    assert ids_in(path.parent / "listings-archive" / "2025-11.csv") == []
    assert everywhere(path)["sreality:2"] == ["listings.csv"]
    back = storage.read_listings(path)["sreality:2"]
    assert back["first_seen_at"] == "2026-09-01T10:00:00+00:00"


def test_an_emptied_archive_is_kept_with_its_header_not_deleted(path):
    """commit_data.sh stages no deletions; a deleted archive would stay in git
    with its rows and bring them back on the next checkout."""
    storage.write_listings({"sreality:1": row(1, "removed", "2026-08-01")}, path)
    storage.write_listings({"sreality:1": row(1, "active", "2026-09-27")}, path)
    emptied = path.parent / "listings-archive" / "2026-08.csv"
    assert emptied.read_text(encoding="utf-8").splitlines() == [",".join(LISTING_FIELDS)]


def test_keep_archives_empties_a_month_that_is_not_on_disk(path):
    storage.write_listings({"sreality:1": row(1)}, path, keep_archives=["2026-07", "junk"])
    archive = path.parent / "listings-archive"
    assert ids_in(archive / "2026-07.csv") == []
    assert not (archive / "junk.csv").exists()


def test_unchanged_files_are_not_rewritten(path):
    rows = {"sreality:1": row(1), "sreality:2": row(2, "removed", "2026-08-01")}
    assert set(storage.write_listings(rows, path)) == {
        path, path.parent / "listings-archive" / "2026-08.csv"}
    assert storage.write_listings(storage.read_listings(path), path) == []


def test_files_that_are_not_months_are_ignored(path):
    storage.write_listings({"sreality:1": row(1)}, path)
    stray = path.parent / "listings-archive" / "notes.csv"
    stray.parent.mkdir()
    stray.write_text("internal_id,status\nx,removed\n", encoding="utf-8")
    assert list(storage.read_listings(path)) == ["sreality:1"]


# --- two copies of one listing ----------------------------------------------


def write_raw(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LISTING_FIELDS)
        w.writeheader()
        w.writerows(rows)


def test_a_duplicate_resolves_to_the_later_sighting(path):
    write_raw(path, [row(1, "active", "2026-10-02")])
    write_raw(path.parent / "listings-archive" / "2026-08.csv", [row(1, "removed", "2026-08-10")])
    assert storage.read_listings(path)["sreality:1"]["status"] == "active"
    assert storage.duplicate_listings(path) == ["sreality:1"]

    storage.write_listings(storage.read_listings(path), path)
    assert storage.duplicate_listings(path) == []
    assert everywhere(path) == {"sreality:1": ["listings.csv"]}


def test_a_stale_copy_in_an_older_archive_loses_to_the_newer_one(path):
    """Revived in October and archived again in November, with the October
    write cut short: the older month is read first and must not win."""
    write_raw(path, [])
    write_raw(path.parent / "listings-archive" / "2026-08.csv", [row(1, "removed", "2026-08-10")])
    write_raw(path.parent / "listings-archive" / "2026-11.csv", [row(1, "removed", "2026-11-03")])
    assert storage.read_listings(path)["sreality:1"]["last_seen_at"] == "2026-11-03"


def test_a_tied_duplicate_resolves_to_the_removed_copy(path):
    """A tie is a listing half way into the archive: missing_2 -> removed does
    not move last_seen_at."""
    write_raw(path, [row(1, "missing_2", "2026-09-20")])
    write_raw(path.parent / "listings-archive" / "2026-09.csv", [row(1, "removed", "2026-09-20")])
    assert storage.read_listings(path)["sreality:1"]["status"] == "removed"


# --- a write that is killed half way ------------------------------------------


class Killed(Exception):
    pass


def writes_until_killed(monkeypatch, allowed):
    real = storage._atomic_write_text
    count = itertools.count()

    def maybe(path, text):
        if next(count) >= allowed:
            raise Killed()
        real(path, text)
    monkeypatch.setattr(storage, "_atomic_write_text", maybe)


@pytest.mark.parametrize("allowed", range(0, 8))
def test_no_interrupted_write_loses_a_row(path, monkeypatch, allowed):
    """Rows move both ways in one write - one archived, one revived, across
    three files - and the write is killed after every possible number of
    file replacements. Every listing must still be readable, in its old or
    new state, never gone."""
    before = {r["internal_id"]: r for r in [
        row(1), row(2, "missing_2", "2026-09-20"), row(3, "removed", "2026-08-15"), row(4, "removed", "2026-07-01"),
    ]}
    storage.write_listings({k: dict(v) for k, v in before.items()}, path)

    after = {k: dict(v) for k, v in before.items()}
    after["sreality:2"]["status"] = "removed"                           # live -> 2026-09
    after["sreality:3"].update(status="active", last_seen_at="2026-09-27")  # 2026-08 -> live
    after["sreality:5"] = row(5)                                         # brand new

    writes_until_killed(monkeypatch, allowed)
    try:
        storage.write_listings({k: dict(v) for k, v in after.items()}, path)
    except Killed:
        pass
    got = storage.read_listings(path)

    for internal_id in before:
        assert internal_id in got, f"lost {internal_id} after {allowed} writes"
        assert got[internal_id]["status"] in (
            before[internal_id]["status"], after[internal_id]["status"])
    # The revived listing is never seen as removed once its live copy is on disk.
    if "sreality:3" in ids_in(path):
        assert got["sreality:3"]["status"] == "active"

    # And the next, uninterrupted write tidies everything up.
    monkeypatch.undo()
    storage.write_listings({k: dict(v) for k, v in after.items()}, path)
    assert storage.read_listings(path) == after
    assert storage.duplicate_listings(path) == []


def test_the_live_file_does_not_grow_with_history(path):
    """The reason for all this: removals leave listings.csv."""
    rows = {}
    for n in range(300):
        month = f"2026-{1 + n % 9:02d}"
        rows[f"sreality:{n}"] = row(n, "removed" if n % 3 else "active", f"{month}-10")
    storage.write_listings(rows, path)
    assert len(ids_in(path)) == 100
    assert len(storage.read_listings(path)) == 300
    assert all(len(v) == 1 for v in everywhere(path).values())
