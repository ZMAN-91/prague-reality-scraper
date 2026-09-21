"""The watchdog on the numbers nobody was reading.

The district cross-check exists to catch a broken coordinate conversion -
which would still produce matches, still with plausible distances, and still
look entirely normal. It reported into a workflow log that is opened only
when something else has already gone wrong.
"""

from __future__ import annotations

from datetime import date

from tools import quality


def history(*values, key="number_pct"):
    return {f"2026-09-{10 + i:02d}": {key: v} for i, v in enumerate(values)}


def test_a_collapsed_cross_check_is_a_fault_from_any_history():
    """An absolute floor catches a collapse even on the very first run, when
    there is nothing to compare against."""
    today = {"district_agreement_pct": 41.0, "district_checked": 9000}
    problems = quality.faults(today, {})
    assert problems and "district agreement" in problems[0]


def test_a_healthy_cross_check_is_not_a_fault():
    assert quality.faults(
        {"district_agreement_pct": 98.4, "district_checked": 9000}, {}) == []


def test_a_sharp_fall_is_a_fault_even_above_the_floor():
    """A relative test catches a slide that never crosses the floor. 95% is
    comfortably above it and 30 points below where this dataset sits."""
    past = history(*[95.0] * 5)
    assert quality.faults({"number_pct": 95.0}, past) == []
    assert quality.faults({"number_pct": 60.0}, past)


def test_ordinary_drift_is_not_a_fault():
    """New listings arrive before they are paired, so coverage dips a little
    every day. A floor set where today sits would fire constantly."""
    past = history(88.0, 87.5, 88.2, 87.9, 88.1)
    assert quality.faults({"number_pct": 85.0}, past) == []


def test_too_little_history_makes_no_relative_claim():
    """Two days is not a median. Comparing against it would turn the second
    run of a new metric into an alarm."""
    assert quality.faults({"number_pct": 20.0}, history(90.0, 90.0)) == []
    assert quality.faults({"number_pct": 20.0}, history(90.0, 90.0, 90.0))


def test_a_metric_missing_today_is_not_a_fall_to_zero():
    """No index built yet means no cross-check, which is not a cross-check
    of zero."""
    assert quality.faults({"gps_pct": 90.0}, history(98.0, 98.0, 98.0,
                                                     key="district_agreement_pct")) == []


def test_every_watched_share_is_compared_not_just_the_first():
    for key in ("gps_pct", "number_pct", "psc_pct"):
        past = history(*[95.0] * 4, key=key)
        assert quality.faults({key: 50.0}, past), key


def test_the_history_is_pruned_to_its_window(tmp_path):
    for n in range(quality.HISTORY_DAYS + 20):
        day = date(2026, 1, 1).toordinal() + n
        quality.record(tmp_path, {"number_pct": 90.0},
                       date.fromordinal(day))
    assert len(quality.load(tmp_path)) == quality.HISTORY_DAYS


def test_unreadable_history_is_treated_as_none(tmp_path):
    path = quality.path_for(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert quality.load(tmp_path) == {}


def test_measuring_an_empty_dataset_does_not_divide_by_zero():
    assert quality.measure({}, None) == {"listings": 0}


def test_the_command_fails_loudly_and_passes_quietly(tmp_path, monkeypatch, capsys):
    rows = {"a": {"internal_id": "a", "lat": "50.0", "lon": "14.4",
                  "cislo_zdroj": "ruian", "psc": "14900",
                  "mestska_cast": "Chodov", "ulice": "Leopoldova"}}
    monkeypatch.setattr(quality.storage, "read_listings", lambda p: rows)
    assert quality.main(["--data-dir", str(tmp_path)]) == 0
    assert "nothing to report" in capsys.readouterr().out

    monkeypatch.setattr(quality, "faults", lambda t, h: ["something broke"])
    assert quality.main(["--data-dir", str(tmp_path)]) == 1
    assert "::error::" in capsys.readouterr().out
