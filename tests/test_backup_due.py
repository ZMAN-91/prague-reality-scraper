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


def run(published, today="2026-09-20", tag=None):
    args = [str(SCRIPT), "--published", published, "--today", today]
    if tag:
        args += ["--tag", tag]
    done = subprocess.run(args, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "NOW": f"{today}T07:15:00Z"})
    assert done.returncode == 0, done.stderr
    return done.stdout.strip(), done.stderr.strip()


def test_no_release_at_all_is_due():
    out, why = run("")
    assert out == "go=true"
    assert "not published yet" in why


def test_a_release_published_today_is_not_due():
    out, why = run("2026-09-20T07:15:00Z")
    assert out == "go=false"
    assert "published today" in why


def test_a_release_from_earlier_in_the_same_week_is_still_due():
    """The bug, stated directly: a Wednesday test release for this week's tag
    is not this week's closing snapshot."""
    out, why = run("2026-09-16T06:07:37Z")
    assert out == "go=true"
    assert "not this week's snapshot" in why


def test_a_release_from_yesterday_is_due():
    """The window runs on one day. Anything older is a different dataset."""
    out, _ = run("2026-09-19T07:15:00Z")
    assert out == "go=true"


def test_a_second_attempt_in_the_same_window_is_stopped():
    """Sixteen attempts arrive across the window; the first one to publish
    must stop the other fifteen, or the archive is built and thrown away
    fifteen times."""
    out, _ = run("2026-09-20T07:02:00Z", today="2026-09-20")
    assert out == "go=false"


def test_the_day_is_read_in_utc_not_local_time():
    """A release published at 23:50 UTC is not the next day's."""
    out, _ = run("2026-09-19T23:50:00Z", today="2026-09-20")
    assert out == "go=true"


def test_the_tag_names_the_iso_week():
    _, why = run("")
    assert "backup-2026-W38" in why
