"""Keep every test out of the repository's own logs/ directory.

tools.lend_gps_from_sreality writes a run-log entry, and its --logs-dir
defaults to storage.LOGS_DIR - the logs/ directory of this checkout. Three
tests called main() without passing one, so every run of the suite appended
three fake "sreality city walk" entries to a tracked file here. They never
reached the data repository, which lives elsewhere, but they sat one
`git add -A` away from being committed as real history.

Fixing those three calls would leave the next test free to make the same
mistake. This makes the default itself safe: the default is read when
main() runs, so pointing it at a temporary directory here covers any tool
and any test, including ones not written yet.
"""

import pytest

from common import storage


@pytest.fixture(autouse=True)
def _logs_never_touch_the_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "LOGS_DIR", tmp_path / "logs-default")
