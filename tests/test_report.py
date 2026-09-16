"""The report, which is the only part of this anybody actually reads.

The things that must not happen: a horizon before the data starts reading as
a change from zero, a chart drawn through two points, and the report quietly
taking a side about whether a rise is good news.
"""

from datetime import datetime, timezone

import pytest

from tools import market, report


def row(den, segment="vse/prodej", okno=1, **values):
    base = {f: "" for f in market.FIELDS}
    base.update({"den": den, "segment": segment, "okno_dnu": okno,
                 "uplnost_dnu": 400, "zmizele_potvrzeno": "ano"})
    base.update(values)
    return base


def days(n, start=1, **values):
    return [row(f"2026-01-{d:02d}", **values) for d in range(start, start + n)]


def render(series, episodes_rows=(), when="2026-01-10"):
    return report.render(list(series), list(episodes_rows),
                         datetime.fromisoformat(when).replace(tzinfo=timezone.utc))


# --- the three windows -------------------------------------------------------


def test_the_flow_table_shows_one_day_seven_and_thirty_side_by_side():
    """Arrivals up today but flat over the month is noise; up over both is a
    trend. Only showing them together makes that visible."""
    series = days(10, nabidka=100)
    series[-1]["nove"] = 4
    series += [row("2026-01-10", okno=7, nove=20, nabidka=100),
               row("2026-01-10", okno=30, nove=50, nabidka=100)]
    flow = [l for l in render(series).splitlines() if l.startswith("| Nové |")][0]
    assert "4" in flow and "20" in flow and "50" in flow


def test_a_level_is_not_repeated_per_window():
    """Supply is the same number whichever window you ask for; printing it
    three times just invites the reader to look for a difference."""
    text = render(days(10, nabidka=100))
    assert len([l for l in text.splitlines() if l.startswith("| Nabídka |")]) == 1


# --- comparisons -------------------------------------------------------------


def test_a_horizon_before_the_data_starts_reads_as_no_comparison():
    text = render(days(2, nabidka=100), when="2026-01-02")
    assert "—" in text


def test_a_real_change_is_shown_with_its_direction():
    series = days(10, nabidka=100)
    series[-1]["nabidka"] = 150
    text = render(series)
    assert "↑" in text and "+50" in text


def test_a_move_under_one_percent_is_not_called_a_move():
    series = days(10, nabidka=1000)
    series[-1]["nabidka"] = 1005
    assert "→" in render(series)


def test_it_never_steps_forward_for_a_comparison():
    series = [row("2026-01-09", nabidka=100), row("2026-01-10", nabidka=200)]
    line = [l for l in render(series).splitlines()
            if l.startswith("| Nabídka |")][0]
    assert line.rstrip().endswith("— |"), line


def test_the_report_never_says_whether_a_rise_is_good():
    series = days(10, cena_median=7_000_000)
    series[-1]["cena_median"] = 9_000_000
    text = render(series).lower().replace("jestli je růst dobrá zpráva", "")
    for judgement in ("dobrá zpráva", "špatn", "zlepš", "zhorš", "varov"):
        assert judgement not in text, f"the report took a side: {judgement}"


# --- charts ------------------------------------------------------------------


def test_a_chart_is_drawn_once_there_is_a_line():
    series = ([row(f"2026-01-{d:02d}", okno=30, nabidka=100 + d)
               for d in range(1, 11)] + days(10, nabidka=100))
    text = render(series)
    assert "```mermaid" in text and "xychart-beta" in text
    assert '"01-01"' in text and '"01-10"' in text


def test_two_points_are_not_a_trend():
    series = ([row("2026-01-09", okno=30, nabidka=100),
               row("2026-01-10", okno=30, nabidka=200)]
              + [row("2026-01-09", nabidka=100), row("2026-01-10", nabidka=200)])
    text = render(series)
    assert "```mermaid" not in text
    assert "tři body nejsou trend" in text


def test_a_long_series_is_thinned_rather_than_drawn_point_by_point():
    series = ([row(f"2026-{m:02d}-{d:02d}", okno=30, nabidka=100 + d)
               for m in range(1, 13) for d in range(1, 29)]
              + [row("2026-12-28", nabidka=100)])
    text = render(series, when="2026-12-28")
    labels = [l for l in text.splitlines() if l.strip().startswith("x-axis")][0]
    assert labels.count('"') // 2 <= report.CHART_MAX_POINTS + 1


def test_a_chart_axis_never_collapses_to_a_single_value():
    """A flat line with low == high makes Mermaid render nothing at all."""
    series = ([row(f"2026-01-{d:02d}", okno=30, nabidka=100) for d in range(1, 11)]
              + days(10, nabidka=100))
    axis = [l for l in render(series).splitlines() if "y-axis" in l][0]
    low, high = axis.split('"')[2].strip().split(" --> ")
    assert float(high) > float(low)


# --- the two artefacts, disclosed --------------------------------------------


def test_a_short_history_is_disclosed_where_durations_are_reported():
    text = render(days(10, nabidka=100, uplnost_dnu=10))
    assert "spodní meze, ne měření" in text
    assert "10 dnů" in text


def test_a_long_history_stops_apologising():
    assert "spodní meze" not in render(days(10, nabidka=100, uplnost_dnu=400))


def test_the_unconfirmed_tail_is_always_disclosed():
    """This one never goes away - the last week is always still settling."""
    assert "podhodnocuje odchody" in render(days(10, nabidka=100, uplnost_dnu=400))


# --- structure ---------------------------------------------------------------


def test_every_indicator_appears():
    series = days(10, **{f: 1 for f, _, _ in report.LEVELS + report.FLOWS})
    text = render(series)
    for _, label, _ in report.LEVELS + report.FLOWS:
        assert label in text, f"{label} missing from the report"


def test_segments_get_their_own_section_with_the_roll_up_first():
    series = [row("2026-01-10", segment=s, nabidka=10)
              for s in ("byt/pronajem", "vse/prodej", "byt/prodej")]
    text = render(series)
    assert text.index("## vse/prodej") < text.index("## byt/prodej")
    assert "## byt/pronajem" in text


def test_notable_properties_are_listed():
    episodes_rows = [
        {"address": "Roztylske namesti", "disposition": "2+kk", "outcome": "active",
         "last_price": "7000000", "days_on_market": "120", "discount_pct": "8.5",
         "attribute_changes": "3"},
        {"address": "Hornomecholupska", "disposition": "3+1", "outcome": "removed",
         "last_price": "9000000", "days_on_market": "4", "discount_pct": "",
         "attribute_changes": "0"},
    ]
    text = render(days(3, nabidka=2), episodes_rows)
    assert "Roztylske namesti" in text and "Hornomecholupska" in text


def test_an_empty_dataset_produces_a_report_rather_than_a_crash():
    assert "Zatím nejsou žádná data" in report.render([], [])


def test_the_report_says_a_departure_is_not_a_sale():
    """The single most tempting wrong conclusion this data invites."""
    assert "neznamená prodej" in render(days(3, nabidka=5))


# --- the first clean run's defects, written down -----------------------------


def test_no_two_rows_carry_the_same_label():
    """A count and a percentage both called "Zlevnilo" is two rows the reader
    has to tell apart by squinting at the unit."""
    labels = [label for _, label, _ in report.LEVELS + report.FLOWS]
    assert len(labels) == len(set(labels)), sorted(labels)


@pytest.mark.parametrize("n,expected", [
    (1, "1 den"), (2, "2 dny"), (4, "4 dny"), (5, "5 dnů"), (30, "30 dnů")])
def test_a_count_of_days_agrees_with_its_number(n, expected):
    assert report.dny(n) == expected


def test_the_header_counts_days_in_czech():
    assert "pokrývá 1 den" in render(days(1), when="2026-01-01")


def test_a_ranking_by_discount_excludes_what_was_never_discounted():
    """The first clean run listed five properties under "biggest discounts"
    at 0.00 % each - with every value tied, the sort returned file order."""
    rows = [{"address": f"a{i}", "disposition": "2+kk", "outcome": "active",
             "last_price": "7000000", "days_on_market": "0",
             "discount_pct": "0.0", "attribute_changes": "0"}
            for i in range(5)]
    text = render(days(3, nabidka=5), rows)
    biggest = text.split("### Největší slevy")[1].split("###")[0]
    assert "_nic_" in biggest, biggest


def test_a_real_discount_is_still_ranked():
    rows = [{"address": "Zlevnena", "disposition": "2+kk", "outcome": "active",
             "last_price": "7000000", "days_on_market": "40",
             "discount_pct": "6.5", "attribute_changes": "0"}]
    assert "Zlevnena" in render(days(3, nabidka=5), rows)


def test_vanishing_on_the_day_it_appeared_is_the_fastest_departure():
    """Zero is the absence of a discount, but it is a real time on market."""
    rows = [{"address": "Bleskem", "disposition": "2+kk", "outcome": "removed",
             "last_price": "7000000", "days_on_market": "0",
             "discount_pct": "", "attribute_changes": "0"}]
    text = render(days(3, nabidka=5), rows)
    assert "Bleskem" in text.split("### Nejrychleji zmizelé")[1]
