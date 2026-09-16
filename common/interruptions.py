"""Telling "something broke" apart from "the hour ended, as planned".

A run of this scraper is not expected to finish. bezrealitky alone is ~1730
listing pages; iDNES rotates through ~257 index pages a day. Stopping when
the wall-clock budget runs out is the design, not a fault - the next run
continues from the cursor (common/progress.py).

Until this module existed every stop went into the same `errors` list, so a
perfectly healthy run that simply ran out of its hour exited non-zero, the
workflow failed on purpose to send a notification, and the notification said
a scrape had failed when nothing had. Worse, a real failure - a portal
returning 500s for an hour - would have looked exactly the same, which makes
the alert worth nothing either way.

So a message is either:

  - an **interruption**: the run stopped somewhere on purpose and will
    continue next time. Recorded, reported, does not fail the run.
  - an **error**: something went wrong that a person should look at.

The distinction is carried in the message itself rather than in a parallel
list, because these messages are produced deep inside three scrapers and
passed up through several layers; a marker travels with the text and cannot
be dropped by a caller that forgot about it.
"""

from __future__ import annotations

# Deliberately ugly and unlikely to occur in a portal's error text.
INTERRUPTION_MARKER = "[planned-stop]"


def interruption(message: str) -> str:
    """Mark a message as an expected stop rather than a failure."""
    return f"{INTERRUPTION_MARKER} {message}"


def is_interruption(message: str) -> bool:
    return message.startswith(INTERRUPTION_MARKER)


def split(messages) -> tuple[list, list]:
    """(real errors, planned interruptions), in their original order."""
    errors, stops = [], []
    for message in messages or ():
        (stops if is_interruption(message) else errors).append(message)
    return errors, stops


def strip_marker(message: str) -> str:
    """The message without its marker, for reading in a log."""
    if is_interruption(message):
        return message[len(INTERRUPTION_MARKER):].strip()
    return message
