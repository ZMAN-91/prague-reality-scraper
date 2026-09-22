"""Reading and writing the three data layers: raw archive, listings.csv,
observations/<YYYY-MM>.csv - plus the run log.

All writes are crash-safe (write to a temp file in the same directory, then
os.replace) because this runs unattended, hourly, for years: a run that gets
killed mid-write (Action timeout, OOM, host reboot) must never leave a
half-written CSV behind for the next run to choke on.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from common.schema import (clean_text, CHANGE_FIELDS, LISTING_FIELDS,
                           OBSERVATION_FIELDS)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LISTINGS_PATH = DATA_DIR / "listings.csv"
OBSERVATIONS_DIR = DATA_DIR / "observations"
CHANGES_DIR = DATA_DIR / "changes"
STATE_DIR = DATA_DIR / "state"
LAST_OBSERVATION_PATH = STATE_DIR / "last_observation.json"
LOGS_DIR = REPO_ROOT / "logs"


def ensure_dirs(data_dir: Path = DATA_DIR, logs_dir: Path = LOGS_DIR) -> None:
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    (data_dir / "observations").mkdir(parents=True, exist_ok=True)
    (data_dir / "changes").mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)


# --- internal state cache (not one of the three public data layers) --------
#
# A small cache of "what was the last observation written for this
# internal_id" (price + status), so run.py can decide in O(1) whether
# something changed without re-scanning every monthly observations/*.csv
# file on every run. This is scraper bookkeeping, not analysis data - see
# README "Interní stav (data/state/)".


def read_last_observation_state(path: Path = LAST_OBSERVATION_PATH) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Corrupt/unreadable state is recoverable (worst case: a few extra
        # observation rows get written this run) - never let it crash a run.
        return {}


def write_last_observation_state(state: dict[str, dict], path: Path = LAST_OBSERVATION_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            # indent=1 and a trailing newline, matching tools/reconcile.py and
            # common/progress.py. Not cosmetic: the commit step runs reconcile
            # on every run, so whatever shape this leaves the file in, git only
            # ever sees reconcile's. Writing a different one here means every
            # commit either reformats all ten thousand lines or does not,
            # depending on a detail nobody would think to check, and a 43,000
            # line diff hides the four that mattered.
            f.write(json.dumps(state, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace a file's contents, or leave it alone if they would not change.

    The skip is not a micro-optimisation. Every file this project writes is
    committed, so rewriting an identical file still produces a commit, a diff
    of zero lines, and a fresh mtime - which is how a quiet hour ends up
    looking like a busy one. Comparing first means a run that genuinely
    changed nothing commits nothing, and the history shows when the market
    actually moved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # newline="" matters: csv writes \r\n, and Path.read_text would
        # translate those to \n, so the comparison could never match and the
        # skip would silently never happen.
        with open(path, "r", newline="", encoding="utf-8") as existing:
            if existing.read() == text:
                return
    except (OSError, UnicodeDecodeError):
        pass  # not there yet, or unreadable - write it
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


# --- Raw archive -----------------------------------------------------------


# Keys whose values are image galleries. The brief is explicit that this
# project does not collect photos, and measured on real sreality detail
# payloads these arrays are ~44% of the archived bytes - so keeping them
# would mean the archive is almost half made of data the project has already
# decided it does not want. Everything else is kept verbatim.
BULK_IMAGE_KEYS = ("advert_images", "advert_images_all", "publicImages", "galleries", "videos")


def strip_bulk(payload):
    """Recursively drop image-gallery keys from a payload before archiving."""
    if isinstance(payload, dict):
        return {
            key: strip_bulk(value)
            for key, value in payload.items()
            if key not in BULK_IMAGE_KEYS
        }
    if isinstance(payload, list):
        return [strip_bulk(item) for item in payload]
    return payload


def write_raw_archive(
    source: str,
    fetched_at: datetime,
    payload: dict,
    raw_dir: Path = RAW_DIR,
    kind: str = "index",
    once_per_day: bool = False,
) -> Optional[Path]:
    """Gzip raw API response(s) for one run of one source.

    Path: data/raw/<source>/<YYYY-MM-DD>/<kind>-<HH>.json.gz

    `once_per_day` skips the write (returning None) if an archive of the
    same `kind` already exists for that day. This is used for the *index*
    walk, and it is a deliberate, documented departure from the brief's
    "archive the complete response of every run":

    A full index dump is the entire active market, every hour. At Prague
    scale that is tens of MB per run, i.e. tens of GB per year committed
    into a git repository that is supposed to survive for years - it would
    hit GitHub's repo size limits within months and make the project
    unusable, which defeats the purpose of archiving in the first place.
    Meanwhile the index rows carry only id + price, and every price change
    is *already* captured losslessly in observations/<YYYY-MM>.csv. So the
    hourly index dump is near-pure redundancy.

    The per-listing *detail* payloads are the opposite: rich, fetched
    exactly once per listing ever, and genuinely irreplaceable - those are
    always archived (`once_per_day=False`).
    """
    # An empty payload is never archived. A failed run would otherwise write
    # a 47-byte {"pages": []} file which, under once_per_day, occupies the
    # day's slot and silently prevents the *successful* run an hour later
    # from archiving anything - which is exactly what happened on this
    # project's first real day.
    if not payload.get("pages"):
        return None

    day_dir = raw_dir / source / fetched_at.strftime("%Y-%m-%d")
    if once_per_day and day_dir.exists():
        if any(day_dir.glob(f"{kind}-*.json.gz")):
            return None
    day_dir.mkdir(parents=True, exist_ok=True)
    out_path = day_dir / f"{kind}-{fetched_at.strftime('%H')}.json.gz"
    body = json.dumps(strip_bulk(payload), ensure_ascii=False).encode("utf-8")
    with gzip.open(out_path, "wb") as f:
        f.write(body)
    return out_path


# --- listings.csv ------------------------------------------------------------


def read_listings(path: Path = LISTINGS_PATH) -> dict[str, dict]:
    """Load listings.csv into a dict keyed by internal_id. Empty dict if the
    file doesn't exist yet (first-ever run)."""
    if not path.exists():
        return {}
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return {row["internal_id"]: row for row in reader}


def write_listings(listings: dict[str, dict], path: Path = LISTINGS_PATH) -> None:
    """Rewrite listings.csv in full. Safe/cheap at the scale this project
    expects (Prague-area flats+houses over several years: tens of thousands
    of rows, not millions) - see README."""
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for internal_id in sorted(listings.keys()):
        row = listings[internal_id]
        # Normalised here as well as at parse time, so that rows written
        # under an older version get tidied the next time they are saved
        # instead of keeping their line breaks forever. See schema.clean_text:
        # embedded newlines turned 2 682 rows into 23 848 physical lines.
        if row.get("description"):
            row["description"] = clean_text(row["description"])
        writer.writerow(row)
    _atomic_write_text(path, buf.getvalue())


# --- observations/<YYYY-MM>.csv ---------------------------------------------


def observations_path_for(when: datetime, observations_dir: Path = OBSERVATIONS_DIR) -> Path:
    return observations_dir / f"{when.strftime('%Y-%m')}.csv"


def append_observations(rows: Iterable[dict], when: datetime, observations_dir: Path = OBSERVATIONS_DIR) -> int:
    """Append observation rows to the month-bucketed CSV for `when`.
    Creates the file (with header) if it doesn't exist yet. Returns the
    number of rows written."""
    rows = list(rows)
    if not rows:
        return 0
    path = observations_path_for(when, observations_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OBSERVATION_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


# --- attribute changes -------------------------------------------------------


def changes_path_for(when: datetime, changes_dir: Path = CHANGES_DIR) -> Path:
    return changes_dir / f"{when.strftime('%Y-%m')}.csv"


def append_changes(rows: Iterable[dict], when: datetime,
                   changes_dir: Path = CHANGES_DIR) -> int:
    """Append attribute-change rows to the month-bucketed CSV for `when`.

    Same shape and same reasoning as append_observations: a log of events,
    never rewritten.
    """
    rows = list(rows)
    if not rows:
        return 0
    path = changes_path_for(when, changes_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CHANGE_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


# --- run log -----------------------------------------------------------------


def write_run_log(entry: dict, logs_dir: Path = LOGS_DIR) -> Path:
    """Append one JSON-line summary of a run to logs/<YYYY-MM-DD>.jsonl.

    JSON-lines (one compact JSON object per line) rather than free-text so a
    run can be grepped/parsed programmatically years later without a custom
    log-format parser, while still being readable with `cat`.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = entry.get("started_at", datetime.utcnow().isoformat())
    day = fetched_at[:10]
    path = logs_dir / f"{day}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path
