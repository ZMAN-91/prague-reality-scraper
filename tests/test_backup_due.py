"""Whether an attempt should build the week's archive.

The check this replaces asked only whether a release tagged for this ISO week
existed. One did, on the Sunday this was written: built on the Wednesday from
a dataset of 3129 listings that had since been rebuilt into 10880. The tag was
for the right week, so the archive was skipped - and would have been skipped
every Sunday after, because the tag was not going anywhere.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "tools" / "backup_due.sh"


def run(uploaded, now="2026-09-21T00:30:00Z"):
    """The script, with the release's asset date supplied instead of fetched.

    `now` is a Monday by default: the week that has ended is 2026-W38.
    """
    done = subprocess.run([str(SCRIPT), "--uploaded", uploaded],
                          capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "NOW": now})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), done.stderr.strip()


def test_no_archive_at_all_is_due():
    out, why = run("")
    assert out == "go=true"
    assert "holds no archive" in why


def test_the_tag_names_the_week_that_ended_not_the_one_running():
    """Monday starts a new ISO week. Tagging with it would name a week six
    days of which have not happened, and the archive would claim to hold a
    week it cannot have seen."""
    _, why = run("")
    assert "backup-2026-W38" in why, why


def test_tuesday_names_the_same_week_as_monday():
    """The retry has to finish Monday's job, not start a second archive one
    week over."""
    _, monday = run("")
    _, tuesday = run("", now="2026-09-22T00:30:00Z")
    assert "backup-2026-W38" in tuesday, tuesday
    assert monday.split()[0] == tuesday.split()[0]


def test_an_archive_under_this_weeks_tag_is_this_weeks_archive():
    out, why = run("2026-09-21T00:45:00Z")
    assert out == "go=false"
    assert "already holds an archive" in why


def test_tuesday_finds_mondays_archive_and_stops():
    out, _ = run("2026-09-21T00:45:00Z", now="2026-09-22T00:30:00Z")
    assert out == "go=false"


def test_a_release_whose_upload_failed_is_due():
    """`gh release view --json assets` comes back empty for a release with no
    assets, which reads the same as no release - and should. A tag with no
    archive under it is not a backup."""
    out, _ = run("")
    assert out == "go=true"


def test_the_following_monday_asks_for_the_next_week():
    _, why = run("", now="2026-09-28T00:30:00Z")
    assert "backup-2026-W39" in why, why


# --- an archive cannot predate the week it claims to hold --------------------

def test_an_archive_uploaded_before_the_week_ended_does_not_count():
    """The real one, found by checking rather than by it failing.

    A manual run on Monday 2026-09-21 - back when the tag was cut from the
    current week rather than the closed one - left a release tagged
    backup-2026-W39 holding 10,943 listings as of that morning. W39 is the
    week ending Sunday 2026-09-27. The run on Monday 2026-09-28 would have
    found that release, called the week done, and skipped it: the week with
    every recent change in it would have had no archive at all.

    This is the same failure as the one in this file's docstring, one layer
    down. Asking "was anything uploaded" was not enough; it has to have been
    uploaded after the week it names.
    """
    out, why = run("2026-09-21T07:57:40Z", now="2026-09-28T04:00:00Z")
    assert out == "go=true"
    assert "before the week it names" in why


def test_an_archive_uploaded_after_the_week_ended_counts():
    """The other side, or the fix would simply rebuild every week for ever
    and the Tuesday retry would take a second copy."""
    out, _ = run("2026-09-28T04:10:00Z", now="2026-09-28T04:00:00Z")
    assert out == "go=false"


def test_the_tuesday_retry_still_finds_mondays_archive():
    """The case the tag exists for, unchanged by the above."""
    out, _ = run("2026-09-28T04:10:00Z", now="2026-09-29T04:00:00Z")
    assert out == "go=false"


def test_an_archive_uploaded_on_the_closing_sunday_itself_counts():
    """The boundary, decided in the lenient direction: a snapshot taken as
    the week closes holds that week. Strictness here would buy nothing and
    would rebuild a perfectly good archive."""
    out, _ = run("2026-09-27T23:00:00Z", now="2026-09-28T04:00:00Z")
    assert out == "go=false"
