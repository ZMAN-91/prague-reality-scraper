"""Where each source got to, so the next run continues instead of restarting.

An hour is not enough to re-read everything this project watches. bezrealitky
alone is ~1728 listing pages; at one request a second that is most of an hour
before iDNES has been touched. So a run is not expected to finish - it is
expected to *stop cleanly and be continued*, which is a different thing and
needs one piece of state: how far round the list the last run got.

## Why a cursor and not a "done" list

The obvious design is to record which listings were refreshed this cycle and
skip them next time. That list is thousands of ids, rewritten every hour, in
a repository where every write is a commit. A cursor is two integers.

It works because the queue is ordered by something stable: listings sorted by
internal_id, which is derived from source+source_id and never changes. Run N
refreshes positions 0..600, records 600; run N+1 starts at 600. When the
cursor passes the end it wraps to zero and the cycle number goes up, so "how
many complete passes have been made" is answerable.

New listings do not obey the cursor. They are fetched first, every run,
ahead of the queue - which is the whole point of watching a market. The
cursor governs only the re-reading of things already known.

## What happens when the list changes underneath the cursor

Listings appear and disappear between runs, so position 600 is not the same
listing it was an hour ago. That is tolerable: the ordering is by a stable
key, so a listing's *position* only drifts by however many ids sort before it
appeared or vanished - a handful, not a reshuffle. A listing can occasionally
be visited twice in a cycle or skipped once. Over a rotation of a few hours
that costs nothing, and the alternative costs 30 KB of churn an hour forever.

The file is small, human-readable JSON, and a corrupt or missing one simply
starts the cycle over rather than stopping a run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

PROGRESS_FILENAME = "progress.json"


def _path(data_dir: Path) -> Path:
    return Path(data_dir) / PROGRESS_FILENAME


def read(data_dir: Path) -> dict:
    """The stored progress, or an empty dict if there is none or it is broken.

    Never raises: a scraper that has run unattended for a year must not be
    stopped by one malformed byte in a bookkeeping file. Losing it costs one
    repeated pass, nothing more.
    """
    try:
        return json.loads(_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write(data_dir: Path, progress: dict) -> None:
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    _path(data_dir).write_text(
        json.dumps(progress, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def cursor_of(progress: dict, source: str) -> int:
    """Position in the queue where the last run stopped."""
    return int(progress.get(source, {}).get("cursor", 0) or 0)


def cycle_of(progress: dict, source: str) -> int:
    """How many complete passes this source has made."""
    return int(progress.get(source, {}).get("cycle", 0) or 0)


def advance(progress: dict, source: str, consumed: int, queue_length: int) -> dict:
    """Move the cursor on by `consumed` items, wrapping at the end of the queue.

    Returns the updated progress dict (mutated in place, and returned so
    callers can chain). `queue_length` of zero leaves the cursor alone rather
    than dividing by it.
    """
    entry = progress.setdefault(source, {})
    cursor = int(entry.get("cursor", 0) or 0) + max(0, consumed)
    cycle = int(entry.get("cycle", 0) or 0)
    if queue_length > 0:
        while cursor >= queue_length:
            cursor -= queue_length
            cycle += 1
    entry["cursor"] = cursor
    entry["cycle"] = cycle
    entry["queue_length"] = queue_length
    return progress


def rotate(items: list, cursor: int) -> list:
    """The queue reordered to start at the cursor and wrap around.

    Returning the whole rotated list rather than a slice means the caller can
    simply take as many as its budget allows and does not need to know how
    many that will be in advance.
    """
    if not items:
        return []
    start = cursor % len(items)
    return items[start:] + items[:start]


def note(progress: dict, source: str, **facts) -> dict:
    """Record extra per-source bookkeeping (a page number, a timestamp).

    Kept deliberately free-form: each source needs to remember something
    slightly different, and inventing a schema for that would be more code
    than the thing it describes.
    """
    progress.setdefault(source, {}).update(facts)
    return progress
