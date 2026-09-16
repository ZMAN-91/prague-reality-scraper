"""What the weekly archive must guarantee.

A backup is only worth the confidence you place in it, so these tests are
mostly about the ways it could quietly be worthless: an archive that is
missing the file you needed, one that restores bytes that are not what was
put in, and one that is empty but looks like a success.
"""

import gzip
import io
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import backup
from tools.backup import BackupError

WHEN = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)

LISTINGS = (
    "internal_id,source,source_id,url,property_type,transaction_type,"
    "disposition,area_m2,price,address,lat,lon,status,first_seen_at,"
    "last_seen_at,cluster_id,dedup_confidence,priority_zone\n"
    "a1,sreality,1,https://x/1,byt,prodej,2+kk,55.0,5000000,"
    "Roztylske namesti 1,50.04,14.48,active,2026-09-15T10:00:00+00:00,"
    "2026-09-16T10:00:00+00:00,c1,high,true\n"
    "a2,idnes,2,https://x/2,byt,prodej,3+1,78.0,8000000,"
    "Hornomecholupska 5,50.05,14.53,active,2026-09-15T10:00:00+00:00,"
    "2026-09-16T10:00:00+00:00,c2,high,true\n"
)


@pytest.fixture
def repo(tmp_path):
    """A miniature of the real tree, including a raw archive that must not
    end up in the backup."""
    (tmp_path / "data/observations").mkdir(parents=True)
    (tmp_path / "data/state").mkdir(parents=True)
    (tmp_path / "data/csv").mkdir(parents=True)
    (tmp_path / "data/raw/sreality/2026-09-15").mkdir(parents=True)
    (tmp_path / "logs").mkdir()

    (tmp_path / "data/listings.csv").write_text(LISTINGS, encoding="utf-8")
    (tmp_path / "data/observations/2026-09.csv").write_text(
        "internal_id,observed_at,price,status\n"
        "a1,2026-09-15T10:00:00+00:00,5000000,active\n",
        encoding="utf-8",
    )
    (tmp_path / "data/state/last_observation.json").write_text(
        json.dumps({"a1": {"price": 5000000}}), encoding="utf-8")
    (tmp_path / "data/progress.json").write_text(
        json.dumps({"idnes": {"week": "2026-W38"}}), encoding="utf-8")
    (tmp_path / "data/csv/souhrn.csv").write_text("kategorie,pocet\nbyt,2\n",
                                                  encoding="utf-8")
    (tmp_path / "logs/2026-09-16.jsonl").write_text('{"run": 1}\n', encoding="utf-8")

    # 200 KB of gzip that must not be copied into the archive every week.
    with gzip.open(tmp_path / "data/raw/sreality/2026-09-15/index-10.json.gz", "wb") as f:
        f.write(json.dumps({"pages": ["x" * 200_000]}).encode("utf-8"))
    return tmp_path


def build(repo, out=None):
    return backup.write_archive(repo, out or repo / "backup", now=WHEN)


# --- the archive holds the dataset ------------------------------------------


def test_the_archive_round_trips(repo):
    path, manifest = build(repo)
    summary = backup.verify_archive(path)
    assert summary["listings"] == 2
    assert summary["week"] == "2026-W38"
    assert manifest["counts"]["observations"] == 1


def test_a_restored_tree_loads_through_the_project_s_own_reader(repo, tmp_path):
    """The point of a backup: the thing that comes out is usable, not merely
    intact."""
    from common import storage

    path, _ = build(repo)
    dest = tmp_path / "restored"
    dest.mkdir()
    with tarfile.open(path) as tar:
        backup._extract_all(tar, dest)
    root = next(p for p in dest.iterdir() if p.is_dir())

    listings = storage.read_listings(root / "data/listings.csv")
    assert set(listings) == {"a1", "a2"}
    assert listings["a1"]["address"] == "Roztylske namesti 1"


def test_every_dataset_layer_is_present(repo):
    path, manifest = build(repo)
    paths = {e["path"] for e in manifest["files"]}
    for expected in (
        "data/listings.csv",
        "data/observations/2026-09.csv",
        "data/state/last_observation.json",
        "data/progress.json",
        "data/csv/souhrn.csv",
        "logs/2026-09-16.jsonl",
    ):
        assert expected in paths, f"{expected} missing from the backup"


def test_the_raw_archive_is_left_out_and_says_so(repo):
    """Excluding it is a decision, not an oversight, so the archive has to
    carry the reason - otherwise a restore looks like data loss."""
    path, manifest = build(repo)
    assert not any(e["path"].startswith("data/raw") for e in manifest["files"])
    assert "data/raw" in manifest["excluded"]
    assert path.stat().st_size < 100_000, "the 200 KB raw blob was copied in"


def test_the_archive_is_self_describing(repo):
    path, _ = build(repo)
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert any(n.endswith("MANIFEST.json") for n in names)
    assert any(n.endswith("RESTORE.txt") for n in names)


def test_a_manifest_is_written_beside_the_archive(repo):
    """So the release page shows what is in there without anyone downloading
    a tarball to find out."""
    path, manifest = build(repo)
    beside = path.parent / "prague-reality-2026-W38.manifest.json"
    assert json.loads(beside.read_text(encoding="utf-8"))["counts"] == manifest["counts"]


# --- the ways it could be worthless -----------------------------------------


def test_an_empty_tree_is_refused(tmp_path):
    """The worst failure mode: a green job and a 200-byte archive. Better a
    loud failure than a backup of nothing."""
    with pytest.raises(BackupError, match="nothing to back up"):
        backup.write_archive(tmp_path, tmp_path / "backup", now=WHEN)


def repack(path, mutate):
    """Rebuild an archive with one member changed, manifest untouched - what
    silent corruption in transit or at rest would look like."""
    with tarfile.open(path) as tar:
        members = [(m, tar.extractfile(m).read() if m.isfile() else b"")
                   for m in tar.getmembers()]
    out = path.with_name("tampered.tar.gz")
    with tarfile.open(out, "w:gz") as tar:
        for info, body in members:
            body = mutate(info.name, body)
            if body is None:
                continue
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out


def test_a_changed_byte_is_caught(repo):
    path, _ = build(repo)
    bad = repack(path, lambda name, body:
                 body.replace(b"5000000", b"9999999")
                 if name.endswith("data/listings.csv") else body)
    with pytest.raises(BackupError, match="corrupt"):
        backup.verify_archive(bad)


def test_a_missing_file_is_caught(repo):
    path, _ = build(repo)
    bad = repack(path, lambda name, body:
                 None if name.endswith("data/observations/2026-09.csv") else body)
    with pytest.raises(BackupError, match="missing from archive"):
        backup.verify_archive(bad)


def test_a_manifest_that_disagrees_with_the_data_is_caught(repo):
    """Hashes can all match and the archive still be wrong - if it was built
    from a half-written listings.csv. The row count is the second opinion."""
    path, _ = build(repo)

    def lie(name, body):
        if name.endswith("MANIFEST.json"):
            manifest = json.loads(body)
            manifest["counts"]["listings"] = 99
            return json.dumps(manifest).encode("utf-8")
        return body

    with pytest.raises(BackupError, match="manifest says 99"):
        backup.verify_archive(repack(path, lie))


def test_a_missing_archive_is_an_error_not_a_pass(tmp_path):
    with pytest.raises(BackupError, match="no such archive"):
        backup.verify_archive(tmp_path / "nope.tar.gz")


def test_an_archive_cannot_write_outside_its_destination(tmp_path):
    """Verification unpacks whatever it is handed, including something a
    third party could have swapped in."""
    path = tmp_path / "evil.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(Exception) as caught:
        backup.verify_archive(path)
    # tarfile's own "data" filter, not a check of our own that could rot.
    assert "outside the destination" in str(caught.value)
    assert not (tmp_path.parent / "escaped.txt").exists()


# --- the weekly part --------------------------------------------------------


def test_the_week_comes_from_the_timestamp(repo):
    """The tag is the ISO week, so a Sunday-night run and the Monday after it
    must not collide - and must not be off by one at the year boundary."""
    sunday = datetime(2027, 1, 3, 5, 30, tzinfo=timezone.utc)
    path, manifest = backup.write_archive(repo, repo / "backup", now=sunday)
    assert manifest["week"] == "2026-W53"
    assert path.name == "prague-reality-2026-W53.tar.gz"


def test_two_weeks_do_not_overwrite_each_other(repo):
    first, _ = backup.write_archive(
        repo, repo / "backup", now=datetime(2026, 9, 13, 5, 30, tzinfo=timezone.utc))
    second, _ = backup.write_archive(
        repo, repo / "backup", now=datetime(2026, 9, 20, 5, 30, tzinfo=timezone.utc))
    assert first != second
    assert first.exists() and second.exists()


def test_the_cli_reports_failure_with_a_non_zero_exit(tmp_path, capsys):
    assert backup.main(["--verify", str(tmp_path / "nope.tar.gz")]) == 1
    assert "BACKUP FAILED" in capsys.readouterr().err


def test_the_cli_builds_and_verifies_in_one_go(repo):
    assert backup.main(["--root", str(repo), "--out", str(repo / "backup")]) == 0
    assert (repo / "backup/prague-reality-2026-W38.tar.gz").exists() or \
        list((repo / "backup").glob("*.tar.gz")), "no archive was written"
