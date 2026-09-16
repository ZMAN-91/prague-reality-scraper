"""Wall-clock and request budgets for one run.

Why this exists: a run writes listings.csv / observations / the run log only
at the very end, so a run that gets killed mid-way (GitHub Actions job
timeout, runner eviction) persists *nothing*. The very first run is exactly
the one at risk: it has to fetch a detail page for every listing it has
never seen, at ~1-2 s each, which for a whole Prague-sized backlog is many
hours - far past any job timeout. Without a budget the project can never
bootstrap: every hourly run would start from zero, burn its timeout, write
nothing, and repeat forever.

So instead: a run takes as much as it can inside its budget, stops
fetching cleanly, and *still writes everything it collected*. The backlog
drains over the following runs. Nothing is lost - a new listing that didn't
get its detail fetch this run simply isn't stored yet, and reappears in the
next index walk.
"""

from __future__ import annotations

import time
from typing import Optional


class Budget:
    """Tracks how much of a run's allowance is left.

    `max_seconds` is wall-clock for the *fetching* phase only - writing,
    dedup and logging happen after it and are fast, but they still need
    headroom before the job's own timeout, so set max_seconds comfortably
    below it (see .github/workflows/scrape.yml).
    """

    def __init__(
        self,
        max_seconds: Optional[float] = None,
        max_new_details: Optional[int] = None,
    ) -> None:
        self.max_seconds = max_seconds
        self.max_new_details = max_new_details
        self._started_at = time.monotonic()
        self._deadline = (
            self._started_at + max_seconds if max_seconds is not None else None
        )
        self.new_details_used = 0
        self.details_skipped = 0
        self.stopped_reasons: list[str] = []

    # --- time ---------------------------------------------------------

    @property
    def deadline(self) -> "Optional[float]":
        """The run's hard stop as time.monotonic(), or None if untimed.

        Handed to common/net.set_deadline so that a single request cannot
        outlast the run by retrying and backing off past it - the budget was
        otherwise only checked between listings.
        """
        return self._deadline

    def elapsed_s(self) -> float:
        return time.monotonic() - self._started_at

    def time_exhausted(self) -> bool:
        if self._deadline is None:
            return False
        if time.monotonic() >= self._deadline:
            self._note("time budget exhausted")
            return True
        return False

    # --- per-listing detail fetches ------------------------------------

    def allow_detail_fetch(self) -> bool:
        """True if one more one-time detail fetch fits in this run's budget.

        Counts the fetch as used when it says yes, so callers must only ask
        immediately before actually fetching.
        """
        if self.time_exhausted():
            self.details_skipped += 1
            return False
        if (
            self.max_new_details is not None
            and self.new_details_used >= self.max_new_details
        ):
            self._note("detail-fetch budget exhausted")
            self.details_skipped += 1
            return False
        self.new_details_used += 1
        return True

    # --- reporting -----------------------------------------------------

    def _note(self, reason: str) -> None:
        if reason not in self.stopped_reasons:
            self.stopped_reasons.append(reason)

    def summary(self) -> dict:
        return {
            "elapsed_s": round(self.elapsed_s(), 1),
            "max_seconds": self.max_seconds,
            "new_details_fetched": self.new_details_used,
            "new_details_skipped_for_budget": self.details_skipped,
            "max_new_details": self.max_new_details,
            "stopped_reasons": list(self.stopped_reasons),
        }
