"""The report, which is the only part of this anybody actually reads.

The things that must not happen: a horizon that lands before the data starts
reading as a change from zero, and the report quietly taking a side about
whether a rise is good news.
"""

from datetime import datetime, timezone

from tools import report


def row(den, segment="vse", **values):
    base = {f: "" for f, _, _, _ in report.INDICATORS}
    base.update({"den": den, "segment": segment})
    base.update({k: v for k, v in values.items()})
    return base


def test_a_horizon_before_the_data_starts_reads_as_no_comparison():
    """Otherwise a two-day-old dataset reports every indicator as having risen
    infinitely from nothing, which is worse than saying nothing."""
    series = [row("2026-01-01", nabidka=100), row("2026-01-02", nabidka=110)]
    text = report.render(series, [], datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert "—" in text, "a missing comparison must render as a dash"
    assert "mládí datasetu" in text, "and the reason has to be stated"


def test_a_real_change_is_shown_with_its_direction():
    series = [row(f"2026-01-{d:02d}", nabidka=100) for d in range(1, 9)]
    series[-1]["nabidka"] = 150
    text = report.render(series, [], datetime(2026, 1, 8, tzinfo=timezone.utc))
    assert "↑" in text and "+50" in text


def test_a_move_under_one_percent_is_not_called_a_move():
    series = [row(f"2026-01-{d:02d}", nabidka=1000) for d in range(1, 9)]
    series[-1]["nabidka"] = 1005
    text = report.render(series, [], datetime(2026, 1, 8, tzinfo=timezone.utc))
    assert "→" in text and "↑" not in text


def test_the_report_never_says_whether_a_rise_is_good():
    series = [row(f"2026-01-{d:02d}", cena_median=7_000_000) for d in range(1, 9)]
    series[-1]["cena_median"] = 8_000_000
    text = report.render(series, [], datetime(2026, 1, 8, tzinfo=timezone.utc))
    for judgement in ("dobrá zpráva", "špatn", "zlepš", "zhorš"):
        assert judgement not in text.lower().replace(
            "jestli je růst dobrá zpráva", ""), f"the report took a side: {judgement}"


def test_an_indicator_too_thin_to_measure_that_day_falls_back_to_an_earlier_one():
    """A quiet Tuesday leaves a hole in one indicator. The comparison steps
    BACK to the last day that has it - never forward, which would compare
    against a reading from after the horizon and quietly shrink the window."""
    series = [row(f"2026-01-{d:02d}", cena_median=7_000_000) for d in range(1, 10)]
    series[0]["cena_median"] = 6_000_000   # 01-01, the fallback
    series[1]["cena_median"] = ""          # 01-02, the exact horizon day, blank
    text = report.render(series, [], datetime(2026, 1, 9, tzinfo=timezone.utc))
    assert "6 000 000" in text


def test_it_never_steps_forward_for_a_comparison():
    """Only the earlier direction is honest: a value from after the horizon
    would report a change over a shorter window than the column claims."""
    series = [row("2026-01-08", cena_median=7_000_000),
              row("2026-01-09", cena_median=9_000_000)]
    text = report.render(series, [], datetime(2026, 1, 9, tzinfo=timezone.utc))
    # 30 days back is before the data; nothing may be borrowed from 01-08.
    horizon_row = [line for line in text.splitlines()
                   if line.startswith("| Medián ceny |")][0]
    assert horizon_row.rstrip().endswith("— |"), horizon_row


def test_every_indicator_appears():
    series = [row("2026-01-01", **{f: 1 for f, _, _, _ in report.INDICATORS})]
    text = report.render(series, [], datetime(2026, 1, 1, tzinfo=timezone.utc))
    for _, label, _, _ in report.INDICATORS:
        assert label in text, f"{label} missing from the report"


def test_segments_get_their_own_table_with_the_combined_one_first():
    series = [row("2026-01-01", segment=s, nabidka=10)
              for s in ("byt/pronajem", "vse/prodej", "byt/prodej")]
    text = report.render(series, [], datetime(2026, 1, 1, tzinfo=timezone.utc))
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
    text = report.render([row("2026-01-01", "vse/prodej", nabidka=2)], episodes_rows,
                         datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "Roztylske namesti" in text
    assert "Hornomecholupska" in text
    assert "Nejrychleji zmizelé" in text


def test_an_empty_dataset_produces_a_report_rather_than_a_crash():
    text = report.render([], [])
    assert "Zatím nejsou žádná data" in text


def test_the_report_says_a_departure_is_not_a_sale():
    """The single most tempting wrong conclusion this data invites."""
    text = report.render([row("2026-01-01", nabidka=5)], [],
                         datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "neznamená prodej" in text
