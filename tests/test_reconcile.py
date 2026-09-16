"""Merging two overlapping runs' data at the row level.

The incident this exists for: the weekly rent run collected for an hour, then
lost every row of it. It and an hourly sale run both wrote listings.csv, the
rent run pushed second, and `git pull --rebase` stopped on a conflict - a
row-keyed CSV has no correct line-level merge. The retry loop then re-ran the
same pull while a rebase was already in progress, so all four attempts failed
identically and the run exited having thrown its work away.
"""

import csv
from pathlib import Path
import io
import json

from common.schema import LISTING_FIELDS
from tools.reconcile import merge_appended, merge_json, merge_listings


def listings_csv(rows):
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        full = {field: "" for field in LISTING_FIELDS}
        full.update(row)
        writer.writerow(full)
    return out.getvalue()


def parse(text):
    return {r["internal_id"]: r for r in csv.DictReader(io.StringIO(text))}


# --- listings: union by key ----------------------------------------------


def test_neither_run_loses_its_rows():
    """The whole point: a sale run and a rent run both keep their work."""
    ours = listings_csv([{"internal_id": "rent1", "transaction_type": "pronajem"}])
    theirs = listings_csv([{"internal_id": "sale1", "transaction_type": "prodej"}])
    merged = parse(merge_listings(ours, theirs))
    assert set(merged) == {"rent1", "sale1"}


def test_a_row_both_runs_have_takes_ours():
    """We just refreshed it; theirs is by definition the older reading."""
    ours = listings_csv([{"internal_id": "a", "status": "active", "area_m2": "55"}])
    theirs = listings_csv([{"internal_id": "a", "status": "removed", "area_m2": "40"}])
    merged = parse(merge_listings(ours, theirs))["a"]
    assert merged["status"] == "active" and merged["area_m2"] == "55"


def test_the_earlier_first_seen_survives():
    """The other run may have seen a listing before we did, and when it first
    appeared is not something to overwrite with a later guess."""
    ours = listings_csv([{"internal_id": "a", "first_seen_at": "2026-09-15T10:00:00+00:00"}])
    theirs = listings_csv([{"internal_id": "a", "first_seen_at": "2026-09-01T08:00:00+00:00"}])
    assert parse(merge_listings(ours, theirs))["a"]["first_seen_at"] == "2026-09-01T08:00:00+00:00"


def test_the_later_last_seen_survives():
    ours = listings_csv([{"internal_id": "a", "last_seen_at": "2026-09-15"}])
    theirs = listings_csv([{"internal_id": "a", "last_seen_at": "2026-09-16"}])
    assert parse(merge_listings(ours, theirs))["a"]["last_seen_at"] == "2026-09-16"


def test_the_output_is_still_a_valid_sorted_listings_file():
    ours = listings_csv([{"internal_id": "b"}, {"internal_id": "z"}])
    theirs = listings_csv([{"internal_id": "a"}, {"internal_id": "m"}])
    text = merge_listings(ours, theirs)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["internal_id"] for r in rows] == ["a", "b", "m", "z"]
    assert list(rows[0]) == LISTING_FIELDS


def test_nothing_committed_yet_leaves_our_file_alone():
    ours = listings_csv([{"internal_id": "a"}])
    assert merge_listings(ours, None) == ours


# --- append-only files ----------------------------------------------------


def test_observations_from_both_runs_are_kept():
    header = "internal_id,observed_at,price,price_per_m2,status"
    ours = f"{header}\nrent1,2026-09-15T20:00,15000,,active\n"
    theirs = f"{header}\nsale1,2026-09-15T19:00,5000000,,active\n"
    merged = merge_appended(ours, theirs, True).splitlines()
    assert merged[0] == header
    assert "sale1,2026-09-15T19:00,5000000,,active" in merged
    assert "rent1,2026-09-15T20:00,15000,,active" in merged


def test_an_observation_recorded_by_both_is_not_duplicated():
    header = "internal_id,observed_at,price,price_per_m2,status"
    line = "a,2026-09-15T19:00,100,,active"
    merged = merge_appended(f"{header}\n{line}\n", f"{header}\n{line}\n", True).splitlines()
    assert merged.count(line) == 1


def test_run_logs_from_both_runs_survive():
    ours = json.dumps({"started_at": "20:00"}) + "\n"
    theirs = json.dumps({"started_at": "19:00"}) + "\n"
    merged = [json.loads(l) for l in merge_appended(ours, theirs, False).splitlines()]
    assert [m["started_at"] for m in merged] == ["19:00", "20:00"]


# --- small json state -----------------------------------------------------


def test_progress_keeps_both_sources_and_ours_wins_a_clash():
    ours = json.dumps({"idnes": {"cursor": 40}})
    theirs = json.dumps({"idnes": {"cursor": 10}, "other": {"cursor": 7}})
    merged = json.loads(merge_json(ours, theirs))
    assert merged["idnes"]["cursor"] == 40
    assert merged["other"]["cursor"] == 7


def test_unparseable_json_is_left_as_ours_rather_than_crashing_the_push():
    """A bookkeeping file must never be the reason an hour of collection is
    lost - which is the exact failure this whole module exists to prevent."""
    assert merge_json('{"a": 1}', "{not json") == '{"a": 1}'


# --- the whole commit step, against a real git repository -----------------


def _git(repo, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


def test_two_overlapping_runs_both_keep_their_work(tmp_path, monkeypatch):
    """The incident, reproduced: a sale run and a rent run that both started
    from the same commit, with the rent run pushing second.

    Before, `git pull --rebase` stopped on a conflict in listings.csv and the
    rent run's hour of collection was discarded. Here the rent run must end
    up with both runs' rows.
    """
    import os
    import subprocess

    from tools.reconcile import reconcile

    repo = tmp_path / "repo"
    (repo / "data" / "observations").mkdir(parents=True)
    (repo / "logs").mkdir()
    _git_init = subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")

    def write_listings(ids_to_last_seen):
        rows = [{"internal_id": i, "first_seen_at": "2026-09-15T19:00:00+00:00",
                 "last_seen_at": seen, "status": "active"}
                for i, seen in sorted(ids_to_last_seen.items())]
        (repo / "data" / "listings.csv").write_text(listings_csv(rows), encoding="utf-8")

    # The commit both runs check out.
    write_listings({"base1": "2026-09-15"})
    (repo / "logs" / "day.jsonl").write_text('{"run":"earlier"}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "branch", "-M", "main")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # The sale run finishes first and commits.
    write_listings({"base1": "2026-09-15", "sale1": "2026-09-15"})
    (repo / "logs" / "day.jsonl").write_text('{"run":"earlier"}\n{"run":"sale"}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "sale")

    # The rent run has been working from `base` all along and knows nothing
    # of the sale commit.
    _git(repo, "checkout", "-q", base)
    write_listings({"base1": "2026-09-16", "rent1": "2026-09-15"})
    (repo / "logs" / "day.jsonl").write_text('{"run":"earlier"}\n{"run":"rent"}\n', encoding="utf-8")

    # ...and the commit step runs: rebase onto the branch without touching
    # the working tree, merge at the row level, commit.
    _git(repo, "reset", "--soft", "main")
    monkeypatch.chdir(repo)
    reconcile("main", Path("data"), Path("logs"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rent")

    merged = parse((repo / "data" / "listings.csv").read_text(encoding="utf-8"))
    assert set(merged) == {"base1", "sale1", "rent1"}, "neither run may lose its rows"
    assert merged["base1"]["last_seen_at"] == "2026-09-16", "the fresher sighting wins"

    logs = (repo / "logs" / "day.jsonl").read_text(encoding="utf-8").splitlines()
    assert logs == ['{"run":"earlier"}', '{"run":"sale"}', '{"run":"rent"}']


def test_it_merges_a_data_repository_checked_out_somewhere_else(tmp_path, monkeypatch):
    """The split layout: the code runs from the public repository's checkout
    while the data lives in a private one cloned into a subdirectory.

    Two things break if this is got wrong, and neither is loud: `git show`
    would run against the wrong repository, and the path it is asked for
    would carry the subdirectory prefix that does not exist inside the data
    repository. Both simply find nothing to merge - and "nothing to merge"
    is exactly what a successful run looks like, so the rows would go missing
    in silence.
    """
    import subprocess

    from tools.reconcile import reconcile

    code = tmp_path / "code"          # the public checkout, cwd for the run
    (code / "tools").mkdir(parents=True)
    store = code / "store"            # the private data repository, cloned in
    (store / "data" / "observations").mkdir(parents=True)
    (store / "logs").mkdir(parents=True)

    subprocess.run(["git", "init", "-q", str(store)], check=True)
    _git(store, "config", "user.email", "t@t")
    _git(store, "config", "user.name", "t")

    def write_listings(ids):
        rows = [{"internal_id": i, "first_seen_at": "2026-09-15T19:00:00+00:00",
                 "last_seen_at": "2026-09-15", "status": "active"} for i in sorted(ids)]
        (store / "data" / "listings.csv").write_text(listings_csv(rows), encoding="utf-8")

    write_listings(["base1"])
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "base")
    _git(store, "branch", "-M", "main")
    base = _git(store, "rev-parse", "HEAD").stdout.strip()

    write_listings(["base1", "sale1"])
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "sale")

    _git(store, "checkout", "-q", base)
    write_listings(["base1", "rent1"])
    _git(store, "reset", "--soft", "main")

    # Run from the code checkout, exactly as the split workflow does.
    monkeypatch.chdir(code)
    notes = reconcile("main", Path("store/data"), Path("store/logs"), Path("store"))

    merged = parse((store / "data" / "listings.csv").read_text(encoding="utf-8"))
    assert set(merged) == {"base1", "sale1", "rent1"}, \
        "the other run's rows were not merged across the repository boundary"
    assert any("data/listings.csv" in n for n in notes)
    assert not any("store/" in n for n in notes), \
        "the note names the file by its path inside the data repository"
