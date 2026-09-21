"""What the donor adverts cost on the days before today.

WHY THIS FILE EXISTS

The sreality adverts used to lend coordinates are read once and dropped -
deliberately, since storing a city's worth would change what this dataset
means. But dropping them also drops their price history, and that history is
what pairs a flat discounted on one portal a day before the other.

On the day between the two discounts the two current prices disagree. Their
histories do not: yesterday both were at the old figure. Matching on "was
ever seen at this exact price" recovers that pair on specific evidence,
where widening the tolerance to 10% recovers it by accepting anything nearby
- including flats that were never the same price at all.

So the adverts stay dropped and only their prices are remembered: a source
id and what it cost, per day, for a short window.

WHY A SHORT WINDOW

A price from three weeks ago is not evidence that two adverts are the same
flat today; it is evidence that two flats were once priced alike, which is
common. The window is long enough to cover a portal lagging its twin by a
few days and short enough that coincidences do not accumulate.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from common import cas

#: How many days of donor prices to keep. A week covers a portal lagging its
#: twin by several days; beyond that a price match is coincidence rather than
#: evidence.
WINDOW_DAYS = 7

FILENAME = "sreality_prices.json"


def path_for(data_dir) -> Path:
    return Path(data_dir) / "state" / FILENAME


def load(data_dir) -> Dict[str, dict]:
    """{day: {source_id: price}}, or {} when there is nothing yet."""
    try:
        with open(path_for(data_dir), encoding="utf-8") as handle:
            remembered = json.load(handle)
    except (OSError, ValueError):
        return {}
    return remembered if isinstance(remembered, dict) else {}


def prune(remembered: Dict[str, dict], today=None) -> Dict[str, dict]:
    """Drop days outside the window.

    Keyed by day rather than by advert so that pruning is a dictionary
    operation and cannot leave one advert's history longer than another's -
    which would make the evidence available for a pair depend on when that
    advert first appeared.
    """
    today = today or cas.today()
    keep = {(today - _days(n)).isoformat() for n in range(WINDOW_DAYS)}
    return {day: prices for day, prices in remembered.items() if day in keep}


def _days(n):
    from datetime import timedelta
    return timedelta(days=n)


def remember(data_dir, donors, today=None) -> int:
    """Record today's donor prices and forget those outside the window.

    Returns how many adverts were recorded.
    """
    today = today or cas.today()
    remembered = prune(load(data_dir), today)
    todays = {}
    for donor in donors:
        source_id = str(donor.get("source_id") or "").strip()
        price = donor.get("price")
        if not source_id or price in (None, ""):
            continue
        try:
            todays[source_id] = float(price)
        except (TypeError, ValueError):
            continue
    remembered[today.isoformat()] = todays

    target = path_for(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(remembered, handle, sort_keys=True)
        handle.write("\n")
    return len(todays)


def history_for(remembered: Dict[str, dict], source_id: str) -> list:
    """Every price this advert was seen at, newest day first."""
    out = []
    for day in sorted(remembered, reverse=True):
        price = (remembered.get(day) or {}).get(str(source_id))
        if price is not None and price not in out:
            out.append(price)
    return out
