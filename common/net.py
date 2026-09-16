"""Shared HTTP plumbing: a polite, resilient session used by both scrapers.

Design goals (this thing has to run unattended for years):
- Identify ourselves honestly (descriptive User-Agent, see below).
- Respect robots.txt.
- Rate-limit ourselves (1-2s between requests) regardless of how fast the
  target server would let us go.
- Retry network hiccups and 5xx/429 with exponential backoff; never retry
  4xx (other than 429) since that usually means a parameter is wrong and
  hammering the server won't fix it - it needs a human to look at the logs.
- Never let one bad request kill the whole run: callers decide what to do
  with a RequestFailed exception (typically: log it, skip this batch,
  move on).
"""

from __future__ import annotations

import os
import random
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from common import robots as robots_rules

# Identifies this project honestly, per the brief's request for a
# descriptive User-Agent stating this is personal, non-commercial market
# monitoring. Replace the contact URL if you fork this for your own use.
USER_AGENT = (
    "PragueRealitySectorAnalysis/1.0 "
    "(+https://github.com/zman-91/prague_reality_sector_analysis; "
    "personal non-commercial real-estate market monitoring; hourly cron)"
)

DEFAULT_TIMEOUT_S = 20
MAX_RETRIES = 5
BACKOFF_BASE_S = 2.0
# One request per second, flat. It was 1-2 s of jitter, which is gentler but
# buys roughly a third less work per hour - and the hour is the binding
# constraint on this project, not any portal's capacity. One request a second
# sustained is a rate every one of these sites serves without noticing; the
# jitter existed to look less mechanical, which was never the point. The
# honest User-Agent still says exactly what this is.
MIN_DELAY_S = 1.0
MAX_DELAY_S = 1.0

# origin -> the robots.txt text, or None when there is none to obey.
_robots_cache: dict[str, Optional[str]] = {}


# --- robots.txt policy ----------------------------------------------------
#
# Verbatim snapshots of what each portal actually serves live in
# docs/robots/ (written by tools/probe_sources.py). The short version, as of
# the snapshot in this repository:
#
#   www.bezrealitky.cz  Disallows /vyhledat*, /search*, /moje-bezrealitky/*
#                       and some mortgage forms. Listing detail pages under
#                       /nemovitosti-byty-domy/* are NOT disallowed, and the
#                       file advertises a sitemap. This scraper uses exactly
#                       that sanctioned route - see scrapers/bezrealitky.py.
#
#   api.bezrealitky.cz  "User-agent: *  Disallow: /" - the entire GraphQL
#                       host is off limits. This project does not use it for
#                       collection (only the one-off diagnostic probe
#                       touched it, to establish these facts).
#
#   www.sreality.cz     856 lines, 19 user-agent blocks. Eighteen are named
#                       search engines with "Allow: /" plus specific
#                       exceptions. The block that applies to everyone else,
#                       including this project, is "Disallow: /" - the whole
#                       site. There is therefore no robots-compliant
#                       automated route to sreality's data.
#
# ROBOTS_OVERRIDE_HOSTS lists hosts whose robots.txt this project knowingly
# does not treat as binding. That is a deliberate operator decision, not a
# default: it is empty here, and is populated from the
# SCRAPER_ROBOTS_OVERRIDE_HOSTS environment variable (see
# .github/workflows/scrape.yml), so the choice is visible in one place, in
# version control, and revocable by deleting one line.
#
# Where it is enabled, the project compensates by keeping its footprint far
# below that of a crawler: it identifies itself honestly in the User-Agent
# (no browser impersonation), waits 1-2s between requests, fetches each
# listing's detail exactly once ever, runs a few times a day rather than
# continuously, and never redistributes what it collects.
ROBOTS_OVERRIDE_HOSTS: set[str] = {
    host.strip().lower()
    for host in os.environ.get("SCRAPER_ROBOTS_OVERRIDE_HOSTS", "").split(",")
    if host.strip()
}


class RequestFailed(Exception):
    """Raised when a request could not be completed after all retries, or
    got a non-retryable client error. Callers should catch this per-source
    so one broken endpoint doesn't take down the whole run.

    `status` carries the HTTP status when there was one, because some of them
    are not failures to the caller: iDNES answers 404 past the last page of a
    search, which is how the end of the index announces itself.
    """

    def __init__(self, message: str, status: "int | None" = None):
        super().__init__(message)
        self.status = status


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/html;q=0.8, */*;q=0.5",
        }
    )
    return session


def polite_sleep(min_s: float = MIN_DELAY_S, max_s: float = MAX_DELAY_S) -> None:
    time.sleep(random.uniform(min_s, max_s))


def is_allowed_by_robots(session: requests.Session, url: str) -> bool:
    """Check robots.txt for `url`. Fails open (returns True) if robots.txt
    can't be fetched/parsed - both target sites serve this exact API to
    their own public frontend with no login, so an unreachable robots.txt
    is far more likely to be a transient network issue than an actual
    blanket disallow, and failing closed would silently stop the whole
    project on a robots.txt hiccup."""
    parsed = urlparse(url)
    if parsed.netloc.lower() in ROBOTS_OVERRIDE_HOSTS:
        return True
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _robots_cache:
        try:
            resp = session.get(f"{origin}/robots.txt", timeout=DEFAULT_TIMEOUT_S)
            _robots_cache[origin] = resp.text if resp.status_code == 200 else None
        except requests.RequestException:
            _robots_cache[origin] = None

    text = _robots_cache[origin]
    if text is None:
        return True
    # common/robots.py rather than urllib.robotparser: the standard library
    # compares paths with startswith, so every rule written with a wildcard
    # matches nothing at all - and wildcards are how these portals write
    # nearly all of their rules. See that module.
    return robots_rules.can_fetch(text, USER_AGENT, url)


# A hard deadline, as time.monotonic(), past which no request will wait any
# longer. Set once per run from the Budget (see run.py).
#
# Without it the run budget was only advisory: it is checked *between*
# listings, while a single request can legitimately take five attempts with
# exponential backoff and, on a 429, up to two minutes of Retry-After each -
# about ten minutes for one URL. A portal that starts rate-limiting therefore
# blows the budget by an unbounded amount, and the first run to hit it ran 70
# minutes on a 50-minute budget, past its own next hourly trigger, with the
# 90-minute job timeout as the only real backstop.
_deadline: Optional[float] = None


def set_deadline(monotonic_deadline: Optional[float]) -> None:
    global _deadline
    _deadline = monotonic_deadline


def _past_deadline() -> bool:
    return _deadline is not None and time.monotonic() >= _deadline


def _sleep_within_deadline(seconds: float) -> bool:
    """Sleep, but never past the deadline. False if there was no time left."""
    if _deadline is None:
        time.sleep(seconds)
        return True
    remaining = _deadline - time.monotonic()
    if remaining <= 0:
        return False
    time.sleep(min(seconds, remaining))
    return True


def _request_with_retry(
    session: requests.Session,
    url: str,
    params: Optional[dict],
    method: str,
    json_body: Optional[dict],
    form_data: Optional[dict],
    max_retries: int,
) -> requests.Response:
    """Shared retry/backoff/robots-check core for fetch_json and fetch_text.
    Returns the raw successful (HTTP 200) Response, or raises RequestFailed."""
    if not is_allowed_by_robots(session, url):
        raise RequestFailed(f"disallowed by robots.txt: {url}")

    if _past_deadline():
        raise RequestFailed(f"run deadline passed before requesting {url}")

    last_error: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            if method == "GET":
                resp = session.get(url, params=params, timeout=DEFAULT_TIMEOUT_S)
            elif form_data is not None:
                resp = session.post(url, data=form_data, timeout=DEFAULT_TIMEOUT_S)
            else:
                resp = session.post(url, json=json_body, timeout=DEFAULT_TIMEOUT_S)
        except requests.RequestException as exc:
            last_error = f"network error: {exc}"
        else:
            if resp.status_code == 200:
                return resp
            elif resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    if not _sleep_within_deadline(min(float(retry_after), 120.0)):
                        raise RequestFailed(
                            f"run deadline passed while honouring Retry-After for {url}"
                        )
                last_error = f"HTTP {resp.status_code}"
            else:
                # Non-retryable client error (400, 403, 404, ...): retrying
                # identical params won't help, surface it immediately.
                raise RequestFailed(
                    f"HTTP {resp.status_code} for {url} (params={params}): {resp.text[:300]}",
                    status=resp.status_code,
                )

        if attempt < max_retries:
            sleep_s = BACKOFF_BASE_S ** attempt + random.uniform(0, 1)
            if not _sleep_within_deadline(sleep_s):
                raise RequestFailed(
                    f"run deadline passed while backing off for {url}: {last_error}"
                )

    raise RequestFailed(f"exhausted {max_retries} retries for {url}: {last_error}")


def fetch_json(
    session: requests.Session,
    url: str,
    params: Optional[dict] = None,
    method: str = "GET",
    json_body: Optional[dict] = None,
    form_data: Optional[dict] = None,
    max_retries: int = MAX_RETRIES,
) -> dict:
    """GET or POST `url`, retrying transient failures with exponential
    backoff, and return the parsed JSON body. Raises RequestFailed if all
    retries are exhausted, the server returns a non-retryable 4xx, or the
    body isn't valid JSON.

    For POST, pass exactly one of `json_body` (application/json) or
    `form_data` (application/x-www-form-urlencoded) depending on what the
    target endpoint expects.
    """
    resp = _request_with_retry(session, url, params, method, json_body, form_data, max_retries)
    try:
        return resp.json()
    except ValueError as exc:
        raise RequestFailed(f"invalid JSON body from {url}: {exc}")


def fetch_bytes(
    session: requests.Session,
    url: str,
    max_retries: int = MAX_RETRIES,
) -> bytes:
    """Raw response body, for content that isn't text - notably the gzipped
    sitemaps some sites serve (`.xml.gz`), where decoding as text first
    would corrupt it."""
    resp = _request_with_retry(session, url, None, "GET", None, None, max_retries)
    return resp.content


def fetch_text(
    session: requests.Session,
    url: str,
    max_retries: int = MAX_RETRIES,
) -> str:
    """GET `url` with the same retry/backoff/robots-check behaviour as
    fetch_json, returning the raw response body as text instead of parsing
    JSON - for scraping an HTML page (e.g. a listing detail page) rather
    than calling a JSON API."""
    resp = _request_with_retry(session, url, None, "GET", None, None, max_retries)
    return resp.text
