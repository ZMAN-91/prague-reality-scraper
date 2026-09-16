"""Shared helpers for reading the dataset back out for analysis.

listings.csv is the normalized source of truth, but it is deliberately
unfriendly to read directly: every value is a string (it came from a CSV),
prices live in a separate observations layer, and the address field is
best-effort text from two different portals. Everything here exists to make
the "just show me the flats on street X" question answerable without
re-deriving that each time.
"""

from __future__ import annotations

import csv
import unicodedata
from pathlib import Path
from typing import Iterable, Optional

from common import storage
from common.schema import STATUS_ACTIVE, is_missing_status


def fold(text: Optional[str]) -> str:
    """Lowercase + strip diacritics, for accent-insensitive matching.

    Czech addresses arrive with and without diacritics depending on the
    portal and the field (sreality's `*_seo_name` values are already
    ASCII-slugged, bezrealitky's come out of a URL slug), so "Nurniho",
    "nurniho" and "Nurniho" all have to match the same street.
    """
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def as_bool(value) -> bool:
    """listings.csv stores booleans as the Python literals True/False."""
    return str(value).strip().lower() in {"true", "1", "yes"}


def as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_listings(data_dir: Path = storage.DATA_DIR) -> list[dict]:
    return list(storage.read_listings(data_dir / "listings.csv").values())


def load_latest_prices(data_dir: Path = storage.DATA_DIR) -> dict[str, dict]:
    """Newest observation per internal_id, across every monthly file.

    Prices deliberately do not live in listings.csv (they change; the
    listing's identity does not), so any view that wants to show a price has
    to fold the observation log back in. Newer files are read last and win.
    """
    latest: dict[str, dict] = {}
    obs_dir = data_dir / "observations"
    if not obs_dir.exists():
        return latest
    for path in sorted(obs_dir.glob("*.csv")):
        with open(path, "r", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                internal_id = row.get("internal_id")
                if not internal_id:
                    continue
                previous = latest.get(internal_id)
                if previous is None or row.get("observed_at", "") >= previous.get("observed_at", ""):
                    latest[internal_id] = row
    return latest


def enrich(listings: Iterable[dict], latest_prices: dict[str, dict]) -> list[dict]:
    """listings + their most recent known price, as one flat row each."""
    out = []
    for row in listings:
        obs = latest_prices.get(row["internal_id"], {})
        enriched = dict(row)
        enriched["price"] = obs.get("price", "")
        enriched["price_per_m2"] = obs.get("price_per_m2", "")
        enriched["price_observed_at"] = obs.get("observed_at", "")
        out.append(enriched)
    return out


def is_live(row: dict) -> bool:
    """Active, or missing but not yet confirmed removed - i.e. what a person
    would call "currently on the market"."""
    status = row.get("status", "")
    return status == STATUS_ACTIVE or is_missing_status(status)


def match_street(row: dict, street_query: str) -> bool:
    """True if a listing's address/url plausibly refers to `street_query`.

    Deliberately checks the URL too: sreality bakes the street into its
    canonical detail URL slug, and for a listing whose address field came
    back thin that slug is the only street signal we have.
    """
    needle = fold(street_query).strip()
    if not needle:
        return False
    haystack = " ".join(
        fold(row.get(field)) for field in ("address", "url", "description")
    )
    return needle in haystack


def write_csv(rows: list[dict], path: Path, fields: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path
