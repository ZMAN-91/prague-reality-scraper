"""The interactive charts page: its data, not its drawing.

The drawing runs in a browser; what can break here is the data it is given -
an indicator left out, the day in progress counted as a day, a level
repeated per window, a gap day passed off as a measurement, or a payload
that closes its own <script> element.
"""

import json
from datetime import datetime, timezone

from tools import dashboard, market


def row(den, segment="byt/prodej", okno=1, **values):
    base = {f: "" for f in market.FIELDS}
    base.update({"den": den, "segment": segment, "okno_dnu": str(okno),
                 "den_uplny": "ano", "den_sebran": "ano"})
    base.update({k: str(v) for k, v in values.items()})
    return base


def data_of(rows):
    return dashboard.build_data(rows, datetime(2026, 1, 5, tzinfo=timezone.utc),
                                comparable_from=__import__("datetime").date(2025, 1, 1))


def test_every_indicator_of_the_report_is_offered():
    from tools import report
    keys = {m["key"] for m in data_of([row("2026-01-01", nabidka=1)])["metrics"]}
    assert keys == {f for f, _, _ in report.LEVELS} | {f for f, _, _ in report.FLOWS}


def test_a_level_is_kept_once_and_a_flow_per_window():
    rows = [row("2026-01-01", nabidka=10, nove=1),
            row("2026-01-01", okno=30, nabidka=10, nove=7)]
    series = data_of(rows)["series"]
    assert "byt/prodej|1|nabidka" in series and "byt/prodej|30|nabidka" not in series
    assert series["byt/prodej|30|nove"] == [7.0]


def test_the_day_in_progress_is_left_out():
    rows = [row("2026-01-01", nabidka=10), row("2026-01-02", nabidka=3, den_uplny="ne")]
    data = data_of(rows)
    assert data["days"] == ["2026-01-01"] and data["latest"] == "2026-01-01"


def test_days_without_the_night_pass_are_listed():
    rows = [row("2026-01-01", nabidka=10), row("2026-01-02", nabidka=3, den_sebran="ne")]
    assert data_of(rows)["gaps"] == ["2026-01-02"]


def test_the_payload_cannot_close_its_script_element():
    data = data_of([row("2026-01-01", nabidka=10)])
    data["generated"] = "</script><script>alert(1)</script>"
    page = dashboard.render(data)
    payload = page.split('id="trh-data">')[1].split("</script>")[0]
    assert json.loads(payload)["generated"].startswith("</script>")


def test_the_fragment_has_no_document_skeleton():
    page = dashboard.render(data_of([row("2026-01-01", nabidka=10)]), fragment=True)
    assert not page.lstrip().lower().startswith("<!doctype")
    assert page.lstrip().startswith("<title>")
