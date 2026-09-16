"""Shared data model and normalization helpers for both scrapers.

This module defines the two CSV layers described in the README:

- ``listings.csv``: one row per unique ad (key: source + source_id), slow-changing fields.
- ``observations/<YYYY-MM>.csv``: append-only log of things that change over time
  (price, status).

Keeping the field lists and status-transition logic here means both scrapers
and ``run.py`` agree on exactly the same shape, so a typo in one place can't
silently corrupt the CSVs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --- Enums -------------------------------------------------------------

PROPERTY_TYPES = {"byt", "dum"}
TRANSACTION_TYPES = {"prodej", "pronajem"}

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
MISSING_STATUS_PREFIX = "missing_"

# How many *consecutive successful* runs a listing may be absent from the
# source before we mark it "removed". Chosen as 3 (not the lower end of the
# 2-3 suggested in the brief): at an hourly cadence this is still only a
# 2-3 hour delay in noticing a real removal, but it comfortably survives a
# single bad API response or a transient block that spans more than one run,
# which is the exact failure mode we're protecting against. See README.
MAX_MISSING_STREAK = 3

# --- Column layouts (must match README exactly) -------------------------

LISTING_FIELDS = [
    "internal_id",
    "source",
    "source_id",
    "url",
    "property_type",
    "transaction_type",
    "disposition",
    "area_m2",
    "floor",
    "lat",
    "lon",
    "address",
    "priority_zone",
    "description",
    "first_seen_at",
    "last_seen_at",
    "status",
    "cluster_id",
    # Was missing from this list until it was caught by a CSV round-trip
    # test: common/dedup.py sets it on every clustered row, but
    # storage.write_listings uses extrasaction="ignore", so it was being
    # silently dropped on write. The brief asks for it explicitly.
    "dedup_confidence",
    "relisted_from",
]

OBSERVATION_FIELDS = [
    "internal_id",
    "observed_at",
    "price",
    "price_per_m2",
    "status",
]


def utcnow_iso() -> str:
    """Timezone-aware UTC timestamp, second precision, ISO-8601."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def day_of(iso_timestamp: str) -> str:
    """The date part (YYYY-MM-DD) of an ISO timestamp.

    Used for `last_seen_at`, which is deliberately stored day-granular - see
    README "Proč je last_seen_at jen datum". Second-granular here would
    rewrite every active row of listings.csv every hour, which makes git's
    delta compression useless and grows the repository by roughly the whole
    file every hour.
    """
    return iso_timestamp[:10]


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse either a full ISO timestamp or a bare YYYY-MM-DD date into a
    timezone-aware UTC datetime.

    Both shapes genuinely occur in listings.csv (`first_seen_at` is a full
    timestamp, `last_seen_at` is a date), and mixing naive and aware
    datetimes raises TypeError on comparison - so every comparison in this
    project goes through here rather than datetime.fromisoformat directly.
    Returns None for anything unparseable, so one malformed historical row
    can never crash a run.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def make_internal_id(source: str, source_id: str) -> str:
    """Deterministic, stable, compact key derived from source + source_id.

    Deterministic (not a random UUID) so re-running the scraper never
    changes a listing's identity, and short enough to keep observations.csv
    compact even after years of hourly runs.
    """
    digest = hashlib.sha1(f"{source}:{source_id}".encode("utf-8")).hexdigest()
    return digest[:16]


def make_cluster_id(member_internal_ids: list[str]) -> str:
    """Deterministic cluster id derived from its (sorted) member ids.

    Only used the first time a cluster is formed; once a cluster_id exists
    on a row it is always reused (see common/dedup.py), so this being
    "unstable" under later membership growth is not a problem in practice.
    """
    joined = ",".join(sorted(member_internal_ids))
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()
    return "clu_" + digest[:10]


def price_per_m2(price: Optional[float], area_m2: Optional[float]) -> Optional[int]:
    if price is None or area_m2 is None or area_m2 <= 0:
        return None
    return round(price / area_m2)


def next_missing_status(current_status: str) -> str:
    """Advance a listing one step through the missing-streak state machine.

    active -> missing_1 -> missing_2 -> ... -> removed (after MAX_MISSING_STREAK
    consecutive misses). Already-removed listings stay removed.
    """
    if current_status == STATUS_REMOVED:
        return STATUS_REMOVED
    if current_status == STATUS_ACTIVE:
        streak = 1
    elif current_status.startswith(MISSING_STATUS_PREFIX):
        streak = int(current_status[len(MISSING_STATUS_PREFIX):]) + 1
    else:
        # Unknown/legacy status value: treat conservatively as a first miss
        # rather than crashing a run that must survive unattended for years.
        streak = 1

    if streak >= MAX_MISSING_STREAK:
        return STATUS_REMOVED
    return f"{MISSING_STATUS_PREFIX}{streak}"


def is_missing_status(status: str) -> bool:
    return status.startswith(MISSING_STATUS_PREFIX)


# --- Normalized scraped record -------------------------------------------


@dataclass
class NormalizedListing:
    """One ad, as extracted from a single scraper run.

    This is the *per-run snapshot* shape produced by scrapers/*.py. run.py
    merges these into the persistent listings.csv / observations CSVs.
    """

    source: str
    source_id: str
    url: str
    property_type: str  # "byt" | "dum"
    transaction_type: str  # "prodej" | "pronajem"
    disposition: Optional[str] = None
    area_m2: Optional[float] = None
    floor: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    address: Optional[str] = None
    description: Optional[str] = None
    price: Optional[int] = None

    # Filled in by common/geo.py after construction.
    priority_zone: bool = False
    in_target_area: bool = True

    # True when this sighting only proves the listing still exists, without
    # re-reading its content. Under the tiered schedule most listings are
    # confirmed present from a source's own index/sitemap without spending a
    # request on them, and a presence-only sighting must refresh last_seen_at
    # without being mistaken for "the price became unknown".
    presence_only: bool = False

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.description = clean_text(self.description)
        self.address = clean_text(self.address)
        if self.property_type not in PROPERTY_TYPES:
            raise ValueError(f"invalid property_type: {self.property_type!r}")
        if self.transaction_type not in TRANSACTION_TYPES:
            raise ValueError(f"invalid transaction_type: {self.transaction_type!r}")

    @property
    def internal_id(self) -> str:
        return make_internal_id(self.source, self.source_id)


def clean_text(value: Optional[str]) -> Optional[str]:
    """Collapse a description's internal whitespace to single spaces.

    Portal descriptions arrive with the line breaks of the form they were
    typed into. Stored verbatim in a CSV they turn one row into twenty, and
    listings.csv went from 2 682 rows to 23 848 physical lines - which makes
    every diff unreadable, makes it impossible to see at a glance which rows
    a run actually changed, and gives git's line-based delta far more work on
    a file rewritten every hour.

    Nothing is lost that this project uses: the text is kept in full, only
    its line breaks become spaces. It is applied at both ends - when a
    scraper builds a listing and when a row is written - so that rows already
    on disk are normalised the next time they are saved rather than staying
    ragged forever.
    """
    if value is None:
        return None
    collapsed = " ".join(str(value).split())
    return collapsed or None


def safe_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        text = str(value).strip().replace(",", ".")
        text = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
        if not text:
            return None
        return float(text)
    except (ValueError, TypeError):
        return None


def safe_int(value) -> Optional[int]:
    f = safe_float(value)
    if f is None:
        return None
    return int(round(f))
