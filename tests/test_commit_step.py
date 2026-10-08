"""The commit step, against real git repositories.

Two runs overlap all the time here - the hourly sale run against the weekly
rent run, and either of them against a code push. What the commit step must
guarantee is narrow and easy to get wrong: the commit carries this run's data
and *nothing else*.

Both failures below are silent. Neither turns a run red, neither shows up in
a log anybody reads; they show up weeks later as data that is not there.
"""

import csv
import io
import os
import subprocess
from pathlib import Path

import pytest

from common.schema import LISTING_FIELDS


def listings_csv(rows):
    """Real columns, not a two-field stand-in: reconcile rewrites the file
    through the full schema, so a cut-down CSV would look changed every run
    and the "nothing changed" case could never be tested."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        full = {field: "" for field in LISTING_FIELDS}
        full.update(row)
        writer.writerow(full)
    return out.getvalue()


def row(internal_id, last_seen="2026-09-16"):
    return {"internal_id": internal_id, "status": "active",
            "first_seen_at": "2026-09-15T19:00:00+00:00", "last_seen_at": last_seen}

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "commit_data.sh"
REPO_ROOT = Path(__file__).resolve().parent.parent


def git(repo, *args, **kwargs):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, **kwargs)


@pytest.fixture
def world(tmp_path):
    """A bare 'GitHub', a clone standing in for the run, and a second clone
    standing in for whatever else pushed while the run was collecting."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True)
    git(seed, "config", "user.email", "t@t")
    git(seed, "config", "user.name", "t")
    (seed / "data" / "observations").mkdir(parents=True)
    (seed / "data" / "raw").mkdir(parents=True)
    (seed / "logs").mkdir()
    (seed / "code.py").write_text("version = 1\n", encoding="utf-8")
    (seed / "data" / "listings.csv").write_text(
        listings_csv([row("base1", "2026-09-15")]), encoding="utf-8")
    (seed / "logs" / "day.jsonl").write_text('{"run":"base"}\n', encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "base")
    git(seed, "push", "-q", "origin", "main")

    run = tmp_path / "run"          # the scrape run's checkout
    subprocess.run(["git", "clone", "-q", str(remote), str(run)], check=True)
    other = tmp_path / "other"      # whoever pushes while it collects
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    for clone in (run, other):
        git(clone, "config", "user.email", "t@t")
        git(clone, "config", "user.name", "t")
    return run, other


def commit_data(run, label="Scrape run", expect_ok=True):
    """Run the real script the workflows run."""
    env = {**os.environ, "DATA_ROOT": str(run),
           "COMMIT_ATTEMPTS": "2", "RETRY_SLEEP": "0"}
    result = subprocess.run(["bash", str(SCRIPT), "main", label],
                            capture_output=True, text=True,
                            cwd=str(REPO_ROOT), env=env)
    if expect_ok:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def collect(run, ids):
    """What a scrape run leaves in the working tree."""
    (run / "data" / "listings.csv").write_text(
        listings_csv([row(i) for i in ids]), encoding="utf-8")


def test_a_code_push_during_the_run_is_not_reverted(world):
    """The 2026-09-16 rent run: it tried to commit a revert of two workflow
    files and was stopped only by a token permission. Had the change been
    Python, it would have gone through."""
    run, other = world

    (other / "code.py").write_text("version = 2\n", encoding="utf-8")
    git(other, "commit", "-qam", "a code change, mid-run")
    git(other, "push", "-q", "origin", "main")

    collect(run, ["base1", "new1"])
    commit_data(run)

    assert git(run, "show", "HEAD:code.py").stdout == "version = 2\n", \
        "the run reverted a code change that landed while it was collecting"
    listings = git(run, "show", "HEAD:data/listings.csv").stdout
    assert "new1" in listings, "and it must still have committed its own data"


def test_it_does_not_delete_data_another_run_added(world):
    """After the reset the index knows about the other run's new raw archive,
    which this run's working tree never had. A plain `git add data/` reads
    that as a deletion."""
    run, other = world

    # git does not track empty directories, so the clone has neither.
    (other / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (other / "data" / "observations").mkdir(parents=True, exist_ok=True)
    (other / "data" / "raw" / "sreality-01.json.gz").write_bytes(b"\x1f\x8b other run")
    (other / "data" / "observations" / "2026-10.csv").write_text(
        "internal_id,price\nx,1\n", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-qm", "another run's data")
    git(other, "push", "-q", "origin", "main")

    collect(run, ["base1", "new1"])
    commit_data(run)

    tracked = git(run, "ls-tree", "-r", "--name-only", "HEAD").stdout.split()
    assert "data/raw/sreality-01.json.gz" in tracked, \
        "the run deleted another run's raw archive"
    assert "data/observations/2026-10.csv" in tracked, \
        "the run deleted another run's observations"


def test_both_runs_rows_survive(world):
    """What reconcile is for, exercised through the real commit step."""
    run, other = world

    (other / "data" / "listings.csv").write_text(
        listings_csv([row("base1", "2026-09-15"), row("sale1")]), encoding="utf-8")
    git(other, "commit", "-qam", "the sale run")
    git(other, "push", "-q", "origin", "main")

    collect(run, ["base1", "rent1"])
    commit_data(run, "Rent scrape run")

    listings = git(run, "show", "HEAD:data/listings.csv").stdout
    assert "sale1" in listings and "rent1" in listings, \
        "neither run may lose its rows"


def test_a_quiet_run_commits_nothing(world):
    """An hour where nothing changed must not produce an empty commit."""
    run, _ = world
    before = git(run, "rev-parse", "HEAD").stdout
    result = commit_data(run)
    assert "No data changes" in result.stdout
    git(run, "fetch", "-q", "origin", "main")
    assert git(run, "rev-parse", "origin/main").stdout == before


def test_a_rejection_that_retrying_cannot_fix_is_not_retried(world, tmp_path):
    """The rent run spent five attempts and two minutes on one permission
    error, printing the same failure five times. A push that fails for a
    reason a retry cannot change must fail once, loudly."""
    run, _ = world
    collect(run, ["base1", "new1"])

    # A pre-receive hook that refuses everything, the way a protected branch
    # or a token without `workflows` permission does.
    hooks = Path(git(run, "config", "--get", "remote.origin.url").stdout.strip()) / "hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'refusing to allow this' >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)

    result = commit_data(run, expect_ok=False)
    assert result.returncode == 1
    assert result.stdout.count("refusing to allow this") == 1, \
        "the same unfixable error was reported more than once"
    assert "retrying cannot fix" in result.stderr


def test_the_workflows_both_use_this_script(tmp_path):
    """The bug survived review because the step was pasted into two files.
    Neither may grow its own copy again."""
    import yaml

    workflows = REPO_ROOT / ".github" / "workflows"
    for name in ("scrape.yml", "scrape-night.yml"):
        spec = yaml.safe_load((workflows / name).read_text(encoding="utf-8"))
        body = " ".join(str(s.get("run", ""))
                        for s in spec["jobs"]["scrape"]["steps"])
        assert "tools/commit_data.sh" in body, f"{name} does not use the script"
        assert "reset --soft" not in body, f"{name} still has an inline --soft reset"


def test_the_report_is_committed_when_it_exists(world):
    """REPORT.md lives at the data repository's root, not under data/, so it
    needs naming separately - and `git add` on a missing path is an error, so
    a run whose report step was skipped must still commit its data."""
    run, _ = world
    collect(run, ["base1", "new1"])
    (run / "REPORT.md").write_text("# Report trhu\n", encoding="utf-8")
    commit_data(run)
    assert "REPORT.md" in git(run, "ls-tree", "-r", "--name-only", "HEAD").stdout


def test_the_weeks_report_copy_is_committed_too(world):
    """reports/<week>.md is what tools/report_due.py looks for. Left out of
    the commit, the week's report is never found and a stale file is."""
    run, _ = world
    collect(run, ["base1", "new1"])
    (run / "REPORT.md").write_text("# Report trhu\n", encoding="utf-8")
    (run / "reports").mkdir()
    (run / "reports" / "2026-W39.md").write_text("# Report trhu\n", encoding="utf-8")
    commit_data(run)
    assert "reports/2026-W39.md" in git(run, "ls-tree", "-r", "--name-only", "HEAD").stdout


def test_a_run_without_a_report_still_commits_its_data(world):
    run, _ = world
    collect(run, ["base1", "new1"])
    assert not (run / "REPORT.md").exists()
    commit_data(run)
    assert "new1" in git(run, "show", "HEAD:data/listings.csv").stdout


# --- the live file and its monthly archives --------------------------------
#
# Removed rows live in data/listings-archive/<YYYY-MM>.csv. Whatever the two
# runs did with a row - archived it, brought it back - what gets committed must
# hold every listing exactly once. Checked on the commit itself, not on the
# working tree: the step stages no deletions, so a file that was only deleted
# locally would still be in git.


def full(internal_id, status="active", last_seen="2026-09-16"):
    r = {field: "" for field in LISTING_FIELDS}
    r.update(internal_id=internal_id, status=status, last_seen_at=last_seen,
             first_seen_at="2026-09-01T10:00:00+00:00")
    return r


def store(clone, rows):
    from common import storage
    storage.write_listings({r["internal_id"]: r for r in rows},
                           clone / "data" / "listings.csv")


def committed(run, tmp_path):
    """The listings as the pushed commit holds them, and where each one is."""
    from common import storage
    out = tmp_path / "committed"
    out.mkdir()
    subprocess.run(f"git -C {run} archive origin/main data | tar -x -C {out}",
                   shell=True, check=True)
    path = out / "data" / "listings.csv"
    where = {}
    for p in storage.listing_files(path):
        for r in csv.DictReader(p.open(encoding="utf-8", newline="")):
            where.setdefault(r["internal_id"], []).append(p.name)
    return storage.read_listings(path), where


def push(clone, message):
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", message)
    git(clone, "push", "-q", "origin", "main")


def test_a_listing_this_run_revived_is_committed_once(world, tmp_path):
    """It was archived when the run checked out; the run saw it again."""
    run, other = world
    store(other, [full("base1"), full("x", "removed", "2026-08-20")])
    push(other, "x archived")
    git(run, "pull", "-q", "origin", "main")

    store(other, [full("base1"), full("x", "removed", "2026-08-20"), full("sale1")])
    push(other, "another run meanwhile")

    store(run, [full("base1"), full("x", "active", "2026-09-27")])
    commit_data(run)

    listings, where = committed(run, tmp_path)
    assert where["x"] == ["listings.csv"]
    assert listings["x"]["status"] == "active"
    assert set(listings) == {"base1", "x", "sale1"}


def test_a_month_the_other_run_created_is_emptied_in_git_too(world, tmp_path):
    """The other run archived x into a month this run's checkout never had;
    this run saw x again. Without writing that month back empty, git keeps
    the other run's copy and x is committed twice."""
    run, other = world
    store(other, [full("base1"), full("x", "removed", "2026-08-20")])
    push(other, "x archived")

    store(run, [full("base1"), full("x", "active", "2026-09-27")])
    commit_data(run)

    listings, where = committed(run, tmp_path)
    assert where["x"] == ["listings.csv"], where
    assert listings["x"]["status"] == "active"


def test_rows_only_the_other_run_archived_survive(world, tmp_path):
    run, other = world
    store(other, [full("base1"), full("gone1", "removed", "2026-07-02"),
                  full("gone2", "removed", "2026-09-01")])
    push(other, "the other run's history")

    store(run, [full("base1"), full("rent1")])
    commit_data(run)

    listings, where = committed(run, tmp_path)
    assert set(listings) == {"base1", "rent1", "gone1", "gone2"}
    assert all(len(files) == 1 for files in where.values()), where
    assert where["gone1"] == ["2026-07.csv"]


# --- rule 3: only what this run changed -------------------------------------
#
# 2026-10-05: the weekly report committed REPORT.md and data/csv/ at 03:02;
# the hourly run that had checked out at 03:01 committed at 03:42 and put
# last week's copies back, because `git add data/ REPORT.md` staged every
# file that differed from the tip - including the stale ones it never wrote.


def test_a_run_does_not_revert_files_it_never_touched(world):
    run, other = world
    (other / "REPORT.md").write_text("# report of last week\n", encoding="utf-8")
    (other / "data" / "csv").mkdir(parents=True, exist_ok=True)
    (other / "data" / "csv" / "trh_denne.csv").write_text("den\n2026-09-27\n", encoding="utf-8")
    push(other, "last week's report")
    git(run, "pull", "-q", "origin", "main")  # the hourly run checks out

    # The weekly report lands while the hourly run is collecting.
    (other / "REPORT.md").write_text("# report of this week\n", encoding="utf-8")
    (other / "data" / "csv" / "trh_denne.csv").write_text("den\n2026-10-04\n", encoding="utf-8")
    push(other, "this week's report")

    collect(run, ["base1", "rent1"])
    commit_data(run)

    assert git(run, "show", "origin/main:REPORT.md").stdout == "# report of this week\n"
    assert "2026-10-04" in git(run, "show", "origin/main:data/csv/trh_denne.csv").stdout
    assert "rent1" in git(run, "show", "origin/main:data/listings.csv").stdout
    # and the working tree now holds the current files for any later step
    assert (run / "REPORT.md").read_text(encoding="utf-8") == "# report of this week\n"


def test_listings_this_run_did_not_touch_are_not_merged_back_stale(world):
    """A run that never wrote listings.csv must not put its checkout's rows
    back over a row the other run changed - "ours wins" is only right for
    rows this run actually refreshed."""
    run, other = world
    store(other, [full("base1", "missing_2", "2026-09-30")])
    push(other, "the other run marked base1 missing")

    (run / "logs" / "day.jsonl").write_text('{"run":"base"}\n{"run":"report"}\n', encoding="utf-8")
    commit_data(run, "Weekly report")

    check = run.parent / "check"
    check.mkdir()
    listings, _ = committed(run, check)
    assert listings["base1"]["status"] == "missing_2"
    assert '"report"' in git(run, "show", "origin/main:logs/day.jsonl").stdout


# --- removing rows -------------------------------------------------------------
#
# 2026-10-05: data maintenance removed 458 excluded bezrealitky rows and the
# commit put every one of them back - the merge was a union with the branch
# tip, and the tip still had them. Nothing can be removed through a union.


def test_a_row_this_run_removed_stays_removed(world, tmp_path):
    run, other = world
    store(other, [full("base1"), full("flatio1"), full("keep1")])
    push(other, "three rows")
    git(run, "pull", "-q", "origin", "main")

    store(run, [full("base1"), full("keep1")])        # this run removes flatio1
    commit_data(run, "Data maintenance")

    listings, _ = committed(run, tmp_path)
    assert set(listings) == {"base1", "keep1"}


def test_removed_here_and_added_there_both_hold(world, tmp_path):
    run, other = world
    store(other, [full("base1"), full("flatio1")])
    push(other, "two rows")
    git(run, "pull", "-q", "origin", "main")

    store(other, [full("base1"), full("flatio1"), full("sale1")])
    push(other, "another run adds sale1 meanwhile")

    store(run, [full("base1")])
    commit_data(run, "Data maintenance")

    listings, _ = committed(run, tmp_path)
    assert set(listings) == {"base1", "sale1"}, "flatio1 removed, sale1 kept"


def test_a_row_the_other_run_removed_is_not_put_back_by_a_run_that_never_touched_it(world, tmp_path):
    run, other = world
    store(other, [full("base1"), full("flatio1")])
    push(other, "two rows")
    git(run, "pull", "-q", "origin", "main")

    store(other, [full("base1")])
    push(other, "maintenance removed flatio1")

    store(run, [full("base1", last_seen="2026-10-05"), full("flatio1")])  # flatio1 untouched
    commit_data(run)

    listings, _ = committed(run, tmp_path)
    assert set(listings) == {"base1"}
    assert listings["base1"]["last_seen_at"] == "2026-10-05"


def test_a_row_the_other_run_removed_but_this_run_saw_again_is_kept(world, tmp_path):
    """Fresh evidence beats an old removal: this run read it today."""
    run, other = world
    store(other, [full("base1"), full("x1")])
    push(other, "two rows")
    git(run, "pull", "-q", "origin", "main")

    store(other, [full("base1")])
    push(other, "removed x1")

    store(run, [full("base1"), full("x1", last_seen="2026-10-05")])
    commit_data(run)

    listings, _ = committed(run, tmp_path)
    assert "x1" in listings


def test_a_failure_on_githubs_side_is_retried(world):
    """2026-10-07 14:31: "remote: fatal error in commit_refs" - GitHub's
    server failing, not the push - was given up as unfixable and forty
    minutes of collection were lost. The next attempt has to go through."""
    run, _ = world
    collect(run, ["base1", "new1"])

    remote = Path(git(run, "config", "--get", "remote.origin.url").stdout.strip())
    hooks = remote / "hooks"
    hooks.mkdir(exist_ok=True)
    marker = remote / "failed-once"
    hook = hooks / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"if [ ! -f '{marker}' ]; then touch '{marker}'; "
        "echo 'fatal error in commit_refs' >&2; exit 1; fi\nexit 0\n",
        encoding="utf-8")
    hook.chmod(0o755)

    result = commit_data(run)
    assert "failed on GitHub's side" in result.stdout
    assert "new1" in git(run, "show", "origin/main:data/listings.csv").stdout
