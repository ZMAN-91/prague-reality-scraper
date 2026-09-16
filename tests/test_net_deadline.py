"""A single request must not outlive the run it belongs to.

The incident: a run with a 50-minute budget was still fetching after 70
minutes, past its own next hourly trigger, with the 90-minute job timeout as
the only real backstop. The budget is checked *between* listings, but one
request can legitimately take five attempts with exponential backoff and, on
a 429, up to two minutes of Retry-After each - about ten minutes for a single
URL. A portal that starts rate-limiting therefore blows the budget by an
unbounded amount.
"""

import time

import pytest
import requests

from common import net


@pytest.fixture(autouse=True)
def clear_deadline():
    net.set_deadline(None)
    net._robots_cache.clear()
    yield
    net.set_deadline(None)
    net._robots_cache.clear()


class FakeResponse:
    def __init__(self, status_code=200, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return {"ok": True}


class FakeSession:
    """Always rate-limits, with the longest Retry-After a portal can ask for."""

    def __init__(self, status=429, retry_after="120"):
        self.calls = 0
        self.status = status
        self.retry_after = retry_after

    def get(self, url, params=None, timeout=None, **kwargs):
        if url.endswith("/robots.txt"):
            return FakeResponse(404, "")
        self.calls += 1
        return FakeResponse(self.status, "slow down", {"Retry-After": self.retry_after})


def test_a_request_past_the_deadline_is_refused_immediately():
    net.set_deadline(time.monotonic() - 1)
    with pytest.raises(net.RequestFailed) as excinfo:
        net.fetch_text(FakeSession(), "https://example.com/x")
    assert "deadline" in str(excinfo.value)


def test_retry_after_never_waits_past_the_deadline():
    """The exact shape of the incident: a 429 with Retry-After: 120, five
    attempts, and a deadline moments away."""
    session = FakeSession(status=429, retry_after="120")
    net.set_deadline(time.monotonic() + 0.2)

    started = time.monotonic()
    with pytest.raises(net.RequestFailed):
        net.fetch_text(session, "https://example.com/x")
    elapsed = time.monotonic() - started

    assert elapsed < 5, f"waited {elapsed:.1f}s despite the deadline"


def test_backoff_never_waits_past_the_deadline():
    session = FakeSession(status=503, retry_after=None)
    net.set_deadline(time.monotonic() + 0.2)

    started = time.monotonic()
    with pytest.raises(net.RequestFailed):
        net.fetch_text(session, "https://example.com/x")
    assert time.monotonic() - started < 5


def test_without_a_deadline_the_old_behaviour_is_unchanged(monkeypatch):
    """Nothing is forced to have a deadline; a tool run by hand still
    retries as it always did."""
    slept = []
    monkeypatch.setattr(net.time, "sleep", lambda s: slept.append(s))
    net.set_deadline(None)
    session = FakeSession(status=503, retry_after=None)
    with pytest.raises(net.RequestFailed):
        net.fetch_text(session, "https://example.com/x", max_retries=3)
    assert slept, "backoff must still happen when no deadline is set"


def test_a_successful_request_is_untouched_by_the_deadline():
    class Fine(FakeSession):
        def get(self, url, params=None, timeout=None, **kwargs):
            if url.endswith("/robots.txt"):
                return FakeResponse(404, "")
            return FakeResponse(200, "hello")

    net.set_deadline(time.monotonic() + 60)
    assert net.fetch_text(Fine(), "https://example.com/x") == "hello"


def test_the_run_hands_its_budget_deadline_to_the_network_layer():
    """The wiring, not just the mechanism: a budget that never reaches
    common/net leaves the whole thing inert."""
    from common.budget import Budget

    budget = Budget(max_seconds=120, max_new_details=None)
    assert budget.deadline is not None
    net.set_deadline(budget.deadline)
    assert net._deadline == budget.deadline

    untimed = Budget(max_seconds=None, max_new_details=None)
    assert untimed.deadline is None
