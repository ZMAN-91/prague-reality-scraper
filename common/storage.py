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
import io
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from common.schema import (clean_text, CHANGE_FIELDS, LISTING_FIELDS,
                           OBSERVATION_FIELDS, STATUS_REMOVED)

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


# listings.csv holds what is live - active and missing_N rows - and every
# removed row lives in a monthly archive beside it, listings-archive/<YYYY-MM>.csv,
# keyed by the month it was last seen. One file for everything ever seen grew
# by ~590 rows a day and would have reached GitHub's 100 MB per-file limit
# within about seven months, at which point every push is refused. A yearly
# archive would cross the same limit; a monthly one stays near 12 MB.
#
# Callers do not see the split. read_listings returns the union and
# write_listings puts each row where its status says it belongs, so a listing
# that comes back after being archived is simply found (same internal_id, same
# first_seen_at, its observations intact), reactivated by run.py like any
# other, and moved back into listings.csv on the same write. Nothing anywhere
# has to know which file a row happens to be in.
#
# Two rules the layout depends on:
#
# * An archive file is never deleted, only emptied to its header. The commit
#   step stages no removals (commit_data.sh, rule 2), so a deleted file would
#   stay in git holding its old rows and resurrect them on the next checkout.
# * A row that changes file is written to its new file before it is removed
#   from the old one. A write killed between the two leaves the row in both,
#   which read_listings resolves, never in neither.

ARCHIVE_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def archive_dir_for(path: Path = LISTINGS_PATH) -> Path:
    """Where the removed rows of `path` live: listings-archive/ beside it."""
    path = Path(path)
    return path.with_name(f"{path.stem}-archive")


def listing_files(path: Path = LISTINGS_PATH) -> list[Path]:
    """The live file (if present) then every monthly archive, oldest first."""
    path = Path(path)
    files = [path] if path.exists() else []
    archive = archive_dir_for(path)
    if archive.is_dir():
        files.extend(sorted(p for p in archive.glob("*.csv")
                            if ARCHIVE_MONTH_RE.match(p.stem)))
    return files


def archive_month(row: dict) -> Optional[str]:
    """The archive a row belongs in, or None for the live file.

    Only removed rows are archived, by the month of their last sighting -
    which does not change while they stay removed, so an archived row stays
    put. A removed row with no usable date stays live rather than guess.
    """
    if row.get("status") != STATUS_REMOVED:
        return None
    month = (row.get("last_seen_at") or "")[:7]
    return month if ARCHIVE_MONTH_RE.match(month) else None


def _read_rows(path: Path) -> list[dict]:
    with open(path, "r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _newer_copy(a: dict, b: dict) -> dict:
    """Which of two copies of one listing to keep.

    Two copies only exist after a write was killed half way (or a hand
    edit). The later sighting wins: that is the reappearance, back in the live
    file while its old copy still sits in the archive. On a tie the removed
    copy wins, because a tie is a listing that was just archived - its
    last_seen_at does not move on the way to `removed`.
    """
    a_seen, b_seen = a.get("last_seen_at") or "", b.get("last_seen_at") or ""
    if a_seen != b_seen:
        return a if a_seen > b_seen else b
    if (b.get("status") == STATUS_REMOVED) != (a.get("status") == STATUS_REMOVED):
        return b if b.get("status") == STATUS_REMOVED else a
    return a


def union_rows(row_sets: Iterable[Iterable[dict]]) -> tuple[dict[str, dict], list[str]]:
    """One dict from several files' rows, and the ids that were in more than one."""
    merged: dict[str, dict] = {}
    duplicates: list[str] = []
    for rows in row_sets:
        for row in rows:
            internal_id = row["internal_id"]
            if internal_id in merged:
                duplicates.append(internal_id)
                merged[internal_id] = _newer_copy(merged[internal_id], row)
            else:
                merged[internal_id] = row
    return merged, sorted(set(duplicates))


def read_listings(path: Path = LISTINGS_PATH) -> dict[str, dict]:
    """Every listing ever seen - listings.csv and its archives - keyed by
    internal_id. Empty dict if there is nothing yet (first-ever run)."""
    merged, _ = union_rows(_read_rows(p) for p in listing_files(path))
    return merged


def duplicate_listings(path: Path = LISTINGS_PATH) -> list[str]:
    """internal_ids present in more than one listings file. Should be empty;
    read_listings copes either way and the next write removes them."""
    return union_rows(_read_rows(p) for p in listing_files(path))[1]


def _render(rows: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in sorted(rows, key=lambda r: r["internal_id"]):
        writer.writerow(row)
    return buf.getvalue()


def write_listings(listings: dict[str, dict], path: Path = LISTINGS_PATH,
                   keep_archives: Iterable[str] = ()) -> list[Path]:
    """Write every listing to the file its status puts it in. Returns the
    files whose contents changed.

    `keep_archives` names archive months that must be written even if no row
    belongs in them and they are not on disk - reconcile passes the months
    the other run committed, so a month this run emptied is emptied in git
    too instead of keeping its rows there.
    """
    path = Path(path)
    archive = archive_dir_for(path)

    new: dict[Path, dict[str, dict]] = {path: {}}
    for internal_id, row in listings.items():
        # Normalised here as well as at parse time, so that rows written
        # under an older version get tidied the next time they are saved
        # instead of keeping their line breaks forever. See schema.clean_text:
        # embedded newlines turned 2 682 rows into 23 848 physical lines.
        if row.get("description"):
            row["description"] = clean_text(row["description"])
        month = archive_month(row)
        target = path if month is None else archive / f"{month}.csv"
        new.setdefault(target, {})[internal_id] = row
    for month in keep_archives:
        if ARCHIVE_MONTH_RE.match(month):
            new.setdefault(archive / f"{month}.csv", {})

    old: dict[Path, dict[str, dict]] = {}
    original: dict[Path, str] = {}
    for existing in listing_files(path):
        new.setdefault(existing, {})  # emptied to a header, never deleted
        with open(existing, "r", newline="", encoding="utf-8") as f:
            original[existing] = f.read()
        old[existing] = {r["internal_id"]: r
                         for r in csv.DictReader(io.StringIO(original[existing]))}

    # Phase one: every file that gains a row is written first, still holding
    # the rows that are about to leave it for another file. After this the
    # moved rows exist twice and nowhere zero times.
    for target, rows in new.items():
        before = old.get(target, {})
        if set(rows) - set(before):
            leaving = {i: r for i, r in before.items()
                       if i not in rows and i in listings}
            _atomic_write_text(target, _render({**leaving, **rows}.values()))

    # Phase two: the exact contents, which drops the stay-behind copies.
    # (_atomic_write_text skips a file that already holds these contents.)
    changed: list[Path] = []
    for target in sorted(new):
        text = _render(new[target].values())
        _atomic_write_text(target, text)
        if text != original.get(target):
            changed.append(target)
    return changed


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
