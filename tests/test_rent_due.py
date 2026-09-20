"""The weekly rent pass decides from the dataset, not from the run history."""

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import rent_due


def write_listings(path: Path, rows: list[dict]) -> Path:
    fields = ["internal_id", "transaction_type", "last_seen_at"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)


def test_no_dataset_at_all_is_due(tmp_path):
    go, reason = rent_due.due(tmp_path / "listings.csv", now=at("2026-09-20T02:15"))
    assert go is True
    assert "no rent" in reason


def test_dataset_without_any_rent_is_due(tmp_path):
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "prodej", "last_seen_at": "2026-09-20T01:24:00+00:00"},
    ])
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is True
    assert "no rent" in reason


def test_rent_collected_this_week_is_not_due(tmp_path):
    # Sunday 20 September is in ISO week 2026-W38, and so is the Wednesday.
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": "2026-09-16T10:30:00+00:00"},
    ])
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is False
    assert "2026-W38" in reason


def test_rent_from_last_week_is_due(tmp_path):
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": "2026-09-13T02:20:00+00:00"},
    ])
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is True
    assert "2026-W37" in reason and "2026-W38" in reason


def test_the_week_starts_on_monday_not_on_sunday(tmp_path):
    """A pass on Sunday must not be read as covering the week that follows.

    Sunday 2026-09-20 closes week 38. If the boundary were Sunday, Monday the
    21st would look already collected and the next pass would be skipped.
    """
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": "2026-09-20T02:20:00+00:00"},
    ])
    assert rent_due.due(path, now=at("2026-09-20T23:59"))[0] is False
    assert rent_due.due(path, now=at("2026-09-21T00:01"))[0] is True


def test_the_newest_rent_sighting_wins(tmp_path):
    """Rent listings from an older week must not mask this week's pass."""
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "old", "transaction_type": "pronajem", "last_seen_at": "2026-09-06T02:20:00+00:00"},
        {"internal_id": "new", "transaction_type": "pronajem", "last_seen_at": "2026-09-16T10:30:00+00:00"},
        {"internal_id": "mid", "transaction_type": "pronajem", "last_seen_at": "2026-09-13T02:20:00+00:00"},
    ])
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is False
    assert "2026-W38" in reason


def test_a_year_boundary_does_not_confuse_the_week(tmp_path):
    """Week 1 of 2027 is not week 1 of 2026."""
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": "2026-01-04T02:20:00+00:00"},
    ])
    # 2026-01-04 is ISO 2026-W01; 2027-01-04 is ISO 2027-W01.
    go, reason = rent_due.due(path, now=at("2027-01-04T02:15"))
    assert go is True


def test_a_blank_or_broken_timestamp_is_ignored_not_trusted(tmp_path):
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": ""},
        {"internal_id": "b", "transaction_type": "pronajem", "last_seen_at": "not-a-date"},
    ])
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is True
    assert "no rent" in reason


def test_an_unreadable_dataset_is_due_rather_than_silently_skipped(tmp_path, monkeypatch):
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "a", "transaction_type": "pronajem", "last_seen_at": "2026-09-16T10:30:00+00:00"},
    ])

    def boom(*args, **kwargs):
        raise OSError("disk went away")

    monkeypatch.setattr(Path, "open", boom)
    go, reason = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is True
    assert "Could not read" in reason


def test_a_sale_listing_seen_today_does_not_count_as_rent(tmp_path):
    """The failure this whole check exists to prevent, stated directly."""
    path = write_listings(tmp_path / "listings.csv", [
        {"internal_id": "s", "transaction_type": "prodej", "last_seen_at": "2026-09-20T01:24:00+00:00"},
        {"internal_id": "r", "transaction_type": "pronajem", "last_seen_at": "2026-09-06T02:20:00+00:00"},
    ])
    go, _ = rent_due.due(path, now=at("2026-09-20T02:15"))
    assert go is True


def test_cli_prints_the_github_output_line(tmp_path, capsys, monkeypatch):
    write_listings(tmp_path / "listings.csv", [])
    monkeypatch.setattr("sys.argv", ["rent_due", "--data-dir", str(tmp_path)])
    assert rent_due.main() == 0
    assert "go=true" in capsys.readouterr().out
