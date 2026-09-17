"""Whether the weekly report has already been written this week.

The scrape guard separates a real run from a refusal by duration. Here the
two are thirty seconds and fifteen, so that rule would eventually mistake one
for the other and stop the report permanently. This asks the report's own
header instead, and these tests are mostly about the ways that header can be
absent or wrong - because every one of them must fall towards writing the
report, never towards silence.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tools import report_due

NOW = datetime(2026, 9, 21, 6, 45, tzinfo=timezone.utc)


def written(tmp_path, days_ago=None, text=None):
    path = tmp_path / "REPORT.md"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    elif days_ago is not None:
        when = NOW - timedelta(days=days_ago)
        path.write_text(
            f"# Report trhu\n\nData k **2026-09-20** · vygenerováno "
            f"{when:%Y-%m-%d %H:%M} UTC · řada pokrývá 5 dnů\n",
            encoding="utf-8")
    return path


def go(path):
    return report_due.due(path, now=NOW)[0]


# --- the ordinary week -------------------------------------------------------


@pytest.mark.parametrize("days,expected", [
    (0.5, False), (3, False), (5.9, False),   # already done this week
    (6.0, True), (7, True), (30, True),       # due
])
def test_the_report_waits_six_days(days, expected, tmp_path):
    assert go(written(tmp_path, days_ago=days)) is expected


def test_the_very_first_report_is_due(tmp_path):
    assert go(tmp_path / "REPORT.md")


# --- every unreadable case must fall towards writing -------------------------


def test_a_report_with_no_header_is_treated_as_due(tmp_path):
    """Refusing on an unreadable header would stop the report for good, and
    the symptom would be silence - the failure mode this whole day was spent
    on."""
    assert go(written(tmp_path, text="# Report trhu\n\nnic tu nestojí\n"))


def test_a_malformed_date_is_treated_as_due(tmp_path):
    assert go(written(tmp_path, text="vygenerováno 2026-13-45 99:99 UTC"))


def test_an_empty_file_is_treated_as_due(tmp_path):
    assert go(written(tmp_path, text=""))


# --- it must actually read the real report's wording -------------------------


def test_it_parses_the_header_the_report_really_writes(tmp_path):
    """Written against tools/report.py's own output, so a change to that
    wording breaks this test rather than the Monday report."""
    from tools import report
    text = report.render([], [], datetime(2026, 9, 15, 6, 45, tzinfo=timezone.utc))
    assert report_due.generated_at(text) is None, "an empty report has no header"

    series = [{f: "" for f in __import__("tools.market", fromlist=["market"]).FIELDS}]
    series[0].update({"den": "2026-09-20", "segment": "vse/prodej", "okno_dnu": 1,
                      "uplnost_dnu": 400, "nabidka": 10})
    text = report.render(series, [], NOW - timedelta(days=7))
    assert report_due.generated_at(text) == NOW - timedelta(days=7)


def test_a_freshly_written_report_is_not_due_again(tmp_path):
    from tools import market, report
    row = {f: "" for f in market.FIELDS}
    row.update({"den": "2026-09-20", "segment": "vse/prodej", "okno_dnu": 1,
                "uplnost_dnu": 400, "nabidka": 10})
    path = tmp_path / "REPORT.md"
    path.write_text(report.render([row], [], NOW), encoding="utf-8")
    assert not go(path)
