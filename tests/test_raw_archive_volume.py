"""What gets written to disk, and how often.

The number that made this worth testing: bezrealitky re-reads all ~1730 of
its Prague listing pages every hour. Archiving each one produced 1.6 MB of
gzip per hour. Gzip cannot be delta-compressed by git, so that is about 14 GB
a year of repository that can never be reclaimed - to store the same
unchanged pages 8760 times each.
"""

import gzip
import json

import pytest

from common.schema import NormalizedListing
from run import merge_source


def listing(source_id, price=5_000_000):
    row = NormalizedListing(
        source="bezrealitky",
        source_id=source_id,
        url=f"https://www.bezrealitky.cz/nemovitosti-byty-domy/{source_id}-x",
        property_type="byt",
        transaction_type="prodej",
        disposition="2+kk",
        area_m2=55.0,
        price=price,
        lat=50.04525,
        lon=14.52430,
        address="Nurmiho, Praha",
    )
    return row


def archived_ids(detail_pages, changed_ids, source="bezrealitky"):
    """The same filter run.py applies, exercised directly."""
    from common.schema import make_internal_id

    return [
        page for page in detail_pages
        if page.get("source_id") is None
        or make_internal_id(source, page["source_id"]) in changed_ids
    ]


def test_a_new_listing_is_archived():
    listings, last_obs, new_ids = {}, {}, []
    stats, observations = merge_source(
        "bezrealitky", [listing("1")], [], listings, last_obs,
        "2026-09-15T10:00:00+00:00", new_ids, set(),
    )
    changed = {row["internal_id"] for row in observations} | set(new_ids)
    pages = [{"kind": "detail", "source_id": "1", "response": {"x": 1}}]
    assert len(archived_ids(pages, changed)) == 1


def test_an_unchanged_re_read_is_not_archived_again():
    """The whole saving: the same page, hour after hour, written once."""
    listings, last_obs, new_ids = {}, {}, []
    merge_source("bezrealitky", [listing("1")], [], listings, last_obs,
                 "2026-09-15T10:00:00+00:00", new_ids, set())

    new_ids.clear()
    stats, observations = merge_source(
        "bezrealitky", [listing("1")], [], listings, last_obs,
        "2026-09-15T11:00:00+00:00", new_ids, set(),
    )
    changed = {row["internal_id"] for row in observations} | set(new_ids)
    pages = [{"kind": "detail", "source_id": "1", "response": {"x": 1}}]
    assert observations == [], "an unchanged listing produces no observation"
    assert archived_ids(pages, changed) == [], "and therefore no second archive copy"


def test_a_price_change_is_archived():
    """The other half: when the portal says something different, keep it."""
    listings, last_obs, new_ids = {}, {}, []
    merge_source("bezrealitky", [listing("1", price=5_000_000)], [], listings, last_obs,
                 "2026-09-15T10:00:00+00:00", new_ids, set())
    new_ids.clear()
    _, observations = merge_source(
        "bezrealitky", [listing("1", price=4_500_000)], [], listings, last_obs,
        "2026-09-15T11:00:00+00:00", new_ids, set(),
    )
    changed = {row["internal_id"] for row in observations} | set(new_ids)
    pages = [{"kind": "detail", "source_id": "1", "response": {"x": 2}}]
    assert len(archived_ids(pages, changed)) == 1


def test_a_page_with_no_source_id_is_kept_rather_than_silently_dropped():
    """Better to store a payload that might be redundant than to lose one
    because a scraper forgot to label it."""
    pages = [{"kind": "detail", "url": "https://example.com/x"}]
    assert len(archived_ids(pages, set())) == 1


def test_every_scraper_labels_its_detail_pages():
    """The filter above silently keeps anything unlabelled, so the labelling
    is what actually has to be right."""
    import inspect

    from scrapers import bezrealitky, idnes

    for module in (bezrealitky, idnes):
        source = inspect.getsource(module)
        for chunk in source.split('"kind": "detail"')[1:]:
            assert "source_id" in chunk[:400], (
                f"{module.__name__} appends a detail page without a source_id"
            )


def test_an_hour_of_unchanged_re_reads_writes_nothing(tmp_path):
    """End to end, at the scale that matters: 200 listings re-read with
    nothing changed must not produce an archive file at all."""
    from common import storage

    listings, last_obs, new_ids = {}, {}, []
    batch = [listing(str(n)) for n in range(200)]
    merge_source("bezrealitky", batch, [], listings, last_obs,
                 "2026-09-15T10:00:00+00:00", new_ids, set())

    new_ids.clear()
    _, observations = merge_source(
        "bezrealitky", batch, [], listings, last_obs,
        "2026-09-15T11:00:00+00:00", new_ids, set(),
    )
    changed = {row["internal_id"] for row in observations} | set(new_ids)
    pages = [{"kind": "detail", "source_id": str(n), "response": {"big": "x" * 5000}}
             for n in range(200)]
    kept = archived_ids(pages, changed)
    assert kept == []
