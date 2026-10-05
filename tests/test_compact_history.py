"""The yearly compaction of the data branch, against real git repositories.

What has to hold: the data is byte for byte what it was, the old history is
in a bundle that restores, nothing a concurrent run pushed is lost, and the
backup tags no longer pin the old history.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "compact_history.sh"
BRANCH = "data"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", BRANCH, str(remote)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(remote), str(work)], check=True)
    git(work, "config", "user.email", "t@t")
    git(work, "config", "user.name", "t")
    git(work, "checkout", "-q", "-b", BRANCH)
    for n in range(5):
        (work / "data.csv").write_text(f"row,{n}\n" * (n + 1), encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", f"run {n}")
        if n == 2:
            git(work, "tag", "backup-2026-W40")
    git(work, "push", "-q", "--tags", "origin", BRANCH)
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f"#!/bin/sh\necho \"$@\" >> {tmp_path}/gh.log\n")
    fake_gh.chmod(0o755)
    return remote, work, tmp_path


def compact(work, tmp_path, **env):
    return subprocess.run(
        ["bash", str(SCRIPT), BRANCH, "2027-01-02"], capture_output=True, text=True,
        env={**os.environ, "DATA_ROOT": str(work), "GH": str(tmp_path / "gh"),
             "BUNDLE_DIR": str(tmp_path), **env})


def test_the_data_stays_and_the_history_becomes_one_commit(world):
    remote, work, tmp = world
    before_tree = git(remote, "rev-parse", f"{BRANCH}^{{tree}}")
    old_tip = git(remote, "rev-parse", BRANCH)

    result = compact(work, tmp)
    assert result.returncode == 0, result.stderr

    assert git(remote, "rev-list", "--count", BRANCH) == "1"
    assert git(remote, "rev-parse", f"{BRANCH}^{{tree}}") == before_tree
    new_tip = git(remote, "rev-parse", BRANCH)
    assert git(remote, "rev-parse", "backup-2026-W40^{commit}") == new_tip, "a tag still pins the old history"
    assert git(remote, "rev-parse", "history-2027-01-02^{commit}") == new_tip

    # The bundle restores the full old history.
    restored = tmp / "restored"
    subprocess.run(["git", "clone", "-q", str(tmp / "prague-reality-history-2027-01-02.bundle"),
                    str(restored)], check=True)
    assert git(restored, "rev-list", "--count", old_tip) == "5"
    # Published with the old tip as target, before anything was replaced.
    log = (tmp / "gh.log").read_text()
    assert f"--target {old_tip}" in log


def test_a_push_in_between_is_not_overwritten(world, tmp_path):
    """The lease is what keeps a concurrent scrape's commit."""
    remote, work, tmp = world
    other = tmp / "other"
    subprocess.run(["git", "clone", "-q", "-b", BRANCH, str(remote), str(other)], check=True)
    git(other, "config", "user.email", "t@t")
    git(other, "config", "user.name", "t")

    # Simulate the race: the script fetches, then a run pushes before it does.
    hook = tmp / "gh"
    hook.write_text(f"""#!/bin/sh
cd {other} && echo late > late.csv && git add -A && git commit -qm late && git push -q origin {BRANCH}
""")
    hook.chmod(0o755)
    result = compact(work, tmp)
    assert result.returncode != 0
    assert "moved while compacting" in result.stderr
    assert git(remote, "rev-list", "--count", BRANCH) == "6", "the late commit must survive"


def test_a_dry_run_changes_nothing(world):
    remote, work, tmp = world
    tip = git(remote, "rev-parse", BRANCH)
    assert compact(work, tmp, DRY_RUN="1").returncode == 0
    assert git(remote, "rev-parse", BRANCH) == tip
    assert not (tmp / "gh.log").exists()


def test_a_shallow_checkout_is_refused(world, tmp_path):
    remote, work, tmp = world
    shallow = tmp / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", "-b", BRANCH, f"file://{remote}",
                    str(shallow)], check=True)
    result = compact(shallow, tmp)
    assert result.returncode != 0 and "shallow" in result.stderr


def test_the_workflow_runs_on_a_full_history_and_is_a_dry_run_by_default():
    import yaml
    spec = yaml.safe_load((SCRIPT.parent.parent / "deploy" / "compact-history.yml").read_text())
    steps = spec["jobs"]["compact"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == 0, "a shallow checkout would bundle nothing"
    assert spec["permissions"]["contents"] == "write"
    compact_step = next(s for s in steps if "compact_history.sh" in s.get("run", ""))
    assert "confirm == 'yes'" in compact_step["env"]["DRY_RUN"]
    assert spec[True]["schedule"][0]["cron"].split()[2:4] == ["2", "1,7"], "2 January and 2 July"


def test_a_bundle_too_large_to_publish_stops_before_anything_changes(world):
    remote, work, tmp = world
    tip = git(remote, "rev-parse", BRANCH)
    result = compact(work, tmp, MAX_BUNDLE_BYTES="10")
    assert result.returncode != 0 and "too large" in result.stderr
    assert git(remote, "rev-parse", BRANCH) == tip
    assert not (tmp / "gh.log").exists()
