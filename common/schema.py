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

from common import cas

# --- Enums -------------------------------------------------------------

PROPERTY_TYPES = {"byt", "dum"}
TRANSACTION_TYPES = {"prodej", "pronajem"}

STATUS_ACTIVE = "active"
STATUS_REMOVED = "removed"
MISSING_STATUS_PREFIX = "missing_"

# How long a listing must be absent before it counts as gone. A week, and
# measured in DAYS rather than in runs.
#
# The run count it replaces was three consecutive sweeps, which at an hourly
# cadence meant about three hours - short enough that any portal hiccup
# lasting a morning produced a wave of false removals, and short enough that
# an advert briefly pulled for editing came back as a "re-listing" of itself.
#
# Days also make the rule mean the same thing for every source, which a run
# count never could: three misses is three hours of sreality and the better
# part of a week of iDNES, whose sweep spans many runs.
#
# Three, not the week it started at. A week was chosen to be safe against a
# portal that goes down for a morning, and it is - but it also meant that on
# a six-day-old dataset every one of 9,715 property episodes read "active",
# because nothing had had time to be confirmed gone. A departure that takes
# longer to confirm than the market takes to move is not a cautious
# measurement, it is a missing one.
#
# The caution is not lost, it moved: an advert spends those days in
# missing_1, missing_2, missing_3, and tools/episodes.py now reports that
# state as `disappearing` with a day count instead of calling it active. So
# a listing that has been gone two days is visible as such while still being
# allowed to come back.
REMOVAL_AFTER_DAYS = 3

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
    # Where lat/lon came from: "" when the portal published them, "cluster"
    # when another advert for the same flat did. Borrowed coordinates are a
    # weaker claim than observed ones and must not read as the same thing.
    "gps_zdroj",
    "address",
    # The same address broken up and folded to ASCII, so a column of them
    # sorts and filters. `address` stays exactly as the portal said it -
    # dedup matches on it, and it is the evidence these four are derived
    # from. See common/address.py, including why cislo_popisne is empty.
    "ulice",
    # Inferred, not published - see common/ruian.py. No portal gives a house
    # number, so this is the nearest address point the state register holds
    # on that street, and the three fields after it are what say how much to
    # believe it: the source, how far the portal's pin was from that point,
    # and how many different houses were about equally close. A number
    # without them would read as fact.
    "cislo_popisne",
    "cislo_orientacni",
    # Which numbering series, in the register's own words: "c.p." for a
    # cislo popisne, "c.ev." for a cislo evidencni. 2.81% of Prague's address
    # points are the latter and they are a different series - evidencni 163
    # is not house 163 - so the column cannot be assumed to hold one kind.
    "cislo_typ",
    # The postcode of the matched address point. Inferred with the number and
    # only as good as it - a pin on the wrong side of a boundary gives the
    # neighbour's - but it is filled for 98.7% of matches and no portal here
    # publishes one at all.
    "psc",
    "cislo_zdroj",
    "cislo_vzdalenost_m",
    "cislo_kandidatu",
    "mestska_cast",
    "obec",
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

# What a property said about itself, when it changed its mind.
#
# Price and status have their own layer (observations); everything else about
# a listing was simply overwritten in listings.csv, so a flat advertised as
# 2+kk and later as 3+1, or one whose area was corrected from 55 to 62 m2,
# left no trace at all. That is worth knowing: an attribute correction is
# usually either a re-listing dressed up as an edit, or a seller repositioning
# - and a rewritten description very often arrives with a price cut.
#
# Append-only, like observations, and for the same reason: it is a log of
# events, and events do not get edited.
CHANGE_FIELDS = [
    "internal_id",
    "changed_at",
    "field",
    "old_value",
    "new_value",
]

# The fields worth logging a change to. Deliberately not `url` (portals
# reshuffle slugs constantly and it means nothing) and not `last_seen_at`
# or `status`, which are not descriptions of the property.
TRACKED_CHANGE_FIELDS = (
    "disposition",
    "area_m2",
    "floor",
    "address",
    "description",
    "priority_zone",
)

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
    """The PRAGUE calendar day of an ISO timestamp, as YYYY-MM-DD.

    Used for `last_seen_at`, which is deliberately stored day-granular - see
    README "Proč je last_seen_at jen datum". Second-granular here would
    rewrite every active row of listings.csv every hour, which makes git's
    delta compression useless and grows the repository by roughly the whole
    file every hour.

    Prague and not UTC since 2026-09-21: this is a dataset about the Prague
    market, so its days are the days a person in Prague lived through. Under
    UTC everything after 22:00 local time was filed under the day before.
    See common/cas.py.
    """
    return cas.day_of(iso_timestamp)


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


def next_missing_status(current_status: str, days_absent: Optional[float] = None,
                        removal_after_days: float = REMOVAL_AFTER_DAYS) -> str:
    """Advance a listing one step through the absence state machine.

    active -> missing_1 -> missing_2 -> ... -> removed, where N is DAYS
    absent and the step to `removed` is taken when `days_absent` reaches
    `removal_after_days`.

    N used to count misses rather than days. It decided nothing - removal was
    already on elapsed time - but it cost a great deal: every increment is a
    different status, and a different status writes an observation row. At an
    hourly cadence a single listing that vanished for its week wrote 168 rows
    on its way to `removed`, one an hour, all saying the same thing. Counting
    days writes 7 of them, and "missing_3" now means what a reader assumes it
    means.

    `days_absent=None` means the caller could not work out how long it has
    been - a row with no usable last_seen_at. That never removes anything:
    not knowing how long something has been gone is not evidence that it is.
    Absent a day count there is nothing to be day-granular about, so those
    fall back to counting misses.
    """
    if current_status == STATUS_REMOVED:
        return STATUS_REMOVED

    if days_absent is not None:
        if days_absent >= removal_after_days:
            return STATUS_REMOVED
        # N is the day of absence: missing_1 on the first, missing_6 on the
        # last before removal. A listing is never "missing_0", so a gap of
        # under a day still counts as the first day.
        return f"{MISSING_STATUS_PREFIX}{max(1, int(days_absent))}"

    if current_status == STATUS_ACTIVE:
        streak = 1
    elif current_status.startswith(MISSING_STATUS_PREFIX):
        streak = int(current_status[len(MISSING_STATUS_PREFIX):]) + 1
    else:
        # Unknown/legacy status value: treat conservatively as a first miss
        # rather than crashing a run that must survive unattended for years.
        streak = 1
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
