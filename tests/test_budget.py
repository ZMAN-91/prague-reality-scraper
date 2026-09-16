"""Tests for common/budget.py.

The budget is what stops a run from being killed mid-way and persisting
nothing, so its edge cases matter: an absent budget must never restrict
anything, and an exhausted one must stay exhausted rather than flapping.
"""

from unittest.mock import patch

from common.budget import Budget


def test_budget_without_limits_never_restricts():
    budget = Budget()
    assert budget.time_exhausted() is False
    for _ in range(1000):
        assert budget.allow_detail_fetch() is True
    assert budget.stopped_reasons == []


def test_detail_budget_allows_exactly_its_quota():
    budget = Budget(max_new_details=3)
    assert [budget.allow_detail_fetch() for _ in range(5)] == [True, True, True, False, False]
    assert budget.new_details_used == 3
    assert budget.details_skipped == 2
    assert "detail-fetch budget exhausted" in budget.stopped_reasons


def test_zero_detail_budget_blocks_everything():
    """A legitimate configuration: index-only runs that just refresh prices
    and statuses without enriching anything new."""
    budget = Budget(max_new_details=0)
    assert budget.allow_detail_fetch() is False
    assert budget.new_details_used == 0


def test_expired_time_budget_blocks_detail_fetches():
    budget = Budget(max_seconds=60)
    with patch("common.budget.time.monotonic", return_value=budget._started_at + 61):
        assert budget.time_exhausted() is True
        assert budget.allow_detail_fetch() is False
    assert "time budget exhausted" in budget.stopped_reasons


def test_time_budget_not_yet_expired_allows_work():
    budget = Budget(max_seconds=600)
    with patch("common.budget.time.monotonic", return_value=budget._started_at + 5):
        assert budget.time_exhausted() is False
        assert budget.allow_detail_fetch() is True


def test_stopped_reasons_are_deduplicated():
    budget = Budget(max_new_details=0)
    for _ in range(5):
        budget.allow_detail_fetch()
    assert budget.stopped_reasons.count("detail-fetch budget exhausted") == 1


def test_summary_is_json_serialisable_and_complete():
    import json

    budget = Budget(max_seconds=30, max_new_details=2)
    budget.allow_detail_fetch()
    summary = budget.summary()
    # It goes straight into the run log, which is JSON-lines.
    json.dumps(summary)
    assert summary["new_details_fetched"] == 1
    assert summary["max_new_details"] == 2
    assert summary["max_seconds"] == 30
    assert "elapsed_s" in summary
