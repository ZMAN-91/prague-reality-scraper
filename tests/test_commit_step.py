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
    for name in ("scrape.yml", "scrape-rent.yml"):
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


def test_a_run_without_a_report_still_commits_its_data(world):
    run, _ = world
    collect(run, ["base1", "new1"])
    assert not (run / "REPORT.md").exists()
    commit_data(run)
    assert "new1" in git(run, "show", "HEAD:data/listings.csv").stdout
