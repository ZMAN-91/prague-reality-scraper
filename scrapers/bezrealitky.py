"""bezrealitky.cz scraper - via the route the site's own robots.txt permits.

## Why this shape

bezrealitky serves its data from a GraphQL API at `api.bezrealitky.cz`,
which a CI-side probe confirmed works and whose schema this project has
read (see docs/robots/ and the notes below). But that host's robots.txt is,
in its entirety:

    User-agent: *
    Disallow: /

The public website is a different matter. `www.bezrealitky.cz/robots.txt`
disallows only `/vyhledat*`, `/search*`, `/moje-bezrealitky/*` and a few
mortgage-form URLs - it does **not** disallow listing detail pages under
`/nemovitosti-byty-domy/*`, and it actively advertises a sitemap:

    Sitemap: https://www.bezrealitky.cz/sitemap/sitemap.xml

So this scraper takes the sanctioned route: read the sitemap the site
publishes for exactly this purpose, then read the listing pages the site
permits. No disallowed host is touched during collection.

## Where the data comes from on those pages

The site is a Next.js app, so every detail page embeds the fully structured
advert object in a `<script id="__NEXT_DATA__">` tag. That means this is not
brittle HTML scraping: the same fields the GraphQL API would return are
present as JSON, including the ones this project cares about and which the
old markers endpoint never had - `street`, `city`, `cityDistrict`, `gps`,
`disposition`, `etage`, `price`, `surface`, `description`.

Field names below were taken from the live GraphQL schema for the `Advert`
type (introspected once by tools/probe_sources.py), so they are the real
names, not guesses. The page JSON uses the same object shape.

## Footprint

One sitemap fetch, then one request per listing - but only for listings
whose sitemap URL slug suggests the target area, and only once per listing
ever for the static fields (run.py re-checks a bounded slice per run for
price changes). That is comparable to a person browsing the site, which is
the intended usage level, and far below a crawler.
"""

from __future__ import annotations

import gzip
import json
import re
from typing import Iterable, Optional

import requests

from common import collection_area, interruptions, net
from common.budget import Budget
from common.geo import is_in_target_area, is_priority_zone
from common.schema import NormalizedListing, safe_float, safe_int

SOURCE_NAME = "bezrealitky"

SITEMAP_INDEX_URL = "https://www.bezrealitky.cz/sitemap/sitemap.xml"
DETAIL_BASE_URL = "https://www.bezrealitky.cz/nemovitosti-byty-domy/"

# Values of the GraphQL enums, confirmed by introspection:
#   OfferType  = UNDEFINED | PRODEJ | PRONAJEM
#   EstateType = UNDEFINED | BYT | DUM | POZEMEK | GARAZ | ...
OFFER_TYPE_TO_TRANSACTION = {"PRODEJ": "prodej", "PRONAJEM": "pronajem"}
ESTATE_TYPE_TO_PROPERTY = {"BYT": "byt", "DUM": "dum"}

# Sitemap URLs look like:
#   /nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-mezilehla-praha
# The slug carries the offer type, estate type and locality, which is enough
# to skip everything outside the target area without fetching it at all -
# the single biggest lever on this scraper's request count.
_SLUG_RE = re.compile(r"/nemovitosti-byty-domy/(\d+)-([a-z0-9\-]+)$", re.I)

# Locality tokens that mean "worth fetching". Prague itself plus the ring of
# municipalities the bounding box covers; the authoritative geography check
# still happens on real GPS after parsing (common/geo.is_in_target_area) -
# this is only a cheap pre-filter to avoid fetching Brno.
TARGET_SLUG_TOKENS = (
    "praha", "modrany", "chodov", "haje", "letnany", "zbraslav", "ricany",
    "jesenice", "pruhonice", "cestlice", "dolni-brezany", "zdiby", "klecany",
    "roztoky", "horomerice", "cernosice", "jinocany", "hostivice", "rudna",
    "jirny", "sulice", "vestec", "psary", "mnisek", "kamenice", "brandys",
    "celakovice", "uvaly", "sestajovice", "zelenec", "mratin", "vodochody",
)

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


_OFFER_IN_SLUG = {"prodej": "prodej", "pronajem": "pronajem", "pronajmu": "pronajem"}
_ESTATE_IN_SLUG = {"bytu": "byt", "byt": "byt", "domu": "dum", "dum": "dum"}


def meta_from_url(url: str) -> Optional[tuple[str, str, str]]:
    """(source_id, property_type, transaction_type) read from a listing URL.

    bezrealitky's slugs carry all three - "1067272-nabidka-prodej-bytu-..." -
    which is what lets the sitemap alone prove a listing still exists without
    spending a request on its page. That matters under the tiered schedule:
    most listings are not re-read in a given hour, and without a cheap
    presence signal their absence from the fetched set would be
    indistinguishable from them being taken down.
    """
    match = _SLUG_RE.search(url)
    if not match:
        return None
    source_id, slug = match.group(1), match.group(2).lower()
    parts = slug.split("-")
    transaction = next((_OFFER_IN_SLUG[p] for p in parts if p in _OFFER_IN_SLUG), None)
    estate = next((_ESTATE_IN_SLUG[p] for p in parts if p in _ESTATE_IN_SLUG), None)
    if not transaction or not estate:
        return None
    return source_id, estate, transaction


def presence_listing(url: str) -> Optional[NormalizedListing]:
    """A sighting that proves a listing is still published, nothing more."""
    meta = meta_from_url(url)
    if meta is None:
        return None
    source_id, property_type, transaction_type = meta
    listing = NormalizedListing(
        source=SOURCE_NAME,
        source_id=source_id,
        url=url,
        property_type=property_type,
        transaction_type=transaction_type,
    )
    listing.presence_only = True
    # Geography was settled the first time this listing was read; a
    # presence-only sighting must not re-litigate it.
    listing.in_target_area = True
    return listing


def _is_probably_in_target_area(slug: str) -> bool:
    return any(token in slug for token in TARGET_SLUG_TOKENS)


def _fetch_xml(session: requests.Session, url: str) -> Optional[str]:
    """Sitemaps are often served gzipped; handle both."""
    try:
        body = net.fetch_bytes(session, url)
    except net.RequestFailed:
        return None
    if body[:2] == b"\x1f\x8b":
        try:
            body = gzip.decompress(body)
        except OSError:
            return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


def iter_sitemap_listing_urls(
    session: requests.Session,
    budget: Optional[Budget] = None,
    max_sitemaps: int = 40,
) -> tuple[list[str], list[str]]:
    """Walk the published sitemap index and return listing detail URLs that
    plausibly fall in the target area.

    Returns (urls, errors). Never raises - a sitemap that fails to parse is
    reported and skipped.
    """
    errors: list[str] = []
    index_xml = _fetch_xml(session, SITEMAP_INDEX_URL)
    if index_xml is None:
        return [], [f"bezrealitky: could not fetch sitemap index {SITEMAP_INDEX_URL}"]

    # The index lists child sitemaps; a flat sitemap lists <url> entries
    # directly. Handle both without requiring an XML schema.
    child_sitemaps = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", index_xml)
    listing_urls: list[str] = []
    seen: set[str] = set()

    def collect(xml: str) -> None:
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml):
            match = _SLUG_RE.search(loc)
            if not match:
                continue
            if not _is_probably_in_target_area(match.group(2).lower()):
                continue
            if loc not in seen:
                seen.add(loc)
                listing_urls.append(loc)

    # The index itself may already contain listing URLs.
    collect(index_xml)

    children = [u for u in child_sitemaps if u.endswith((".xml", ".xml.gz")) and u != SITEMAP_INDEX_URL]
    for child in children[:max_sitemaps]:
        if budget is not None and budget.time_exhausted():
            errors.append(interruptions.interruption(
                "bezrealitky: sitemap walk stopped - run time budget exhausted"))
            break
        net.polite_sleep()
        child_xml = _fetch_xml(session, child)
        if child_xml is None:
            errors.append(f"bezrealitky: could not fetch sitemap {child}")
            continue
        collect(child_xml)

    return listing_urls, errors


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)


def extract_advert_json(html: str) -> Optional[dict]:
    """Pull the advert object out of a detail page's __NEXT_DATA__ blob.

    Next.js serialises the page's props as JSON, so the structured advert -
    the same shape the GraphQL API returns - is right there. The exact nesting
    varies between Next.js versions and page types, so rather than hard-coding
    a path, walk the tree and take the first object that looks like an advert
    (has an id and at least one advert-specific field).
    """
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None

    marker_fields = {"offerType", "estateType", "surface", "disposition", "uri"}

    best: Optional[dict] = None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("id") is not None and len(marker_fields & node.keys()) >= 2:
                # Prefer the richest candidate - nested "related advert"
                # stubs carry fewer fields than the page's own advert.
                if best is None or len(node) > len(best):
                    best = node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return best


def _gps_pair(advert: dict) -> tuple[Optional[float], Optional[float]]:
    gps = advert.get("gps") or {}
    if not isinstance(gps, dict):
        return None, None
    lat = safe_float(gps.get("lat"))
    # GraphQL's GPSPoint uses lng; tolerate lon too.
    lon = safe_float(gps.get("lng") if gps.get("lng") is not None else gps.get("lon"))
    return lat, lon


_DISPOSITION_RE = re.compile(r"(\d)\s*[+_\-]?\s*(kk|1)\b", re.I)


def normalize_disposition(raw) -> Optional[str]:
    """Map bezrealitky's Disposition enum onto sreality's "N+kk" spelling.

    Cross-source clustering requires an exact string match on disposition
    (see common/dedup.py), so this mapping is what lets the same flat listed
    on both portals actually cluster. Unrecognised values fall through as
    lowercased raw text rather than being dropped.
    """
    if not raw:
        return None
    text = str(raw)
    match = _DISPOSITION_RE.search(text.replace("DISP_", ""))
    if match:
        return f"{match.group(1)}+{match.group(2).lower()}"
    if "GARSON" in text.upper():
        return "1+kk"
    return text.strip().lower() or None


def _address_of(advert: dict) -> Optional[str]:
    """A real address, assembled from the structured fields the page carries
    (street/houseNumber/cityDistrict/city) - not guessed from a URL slug the
    way the previous, now-dead endpoint forced."""
    street = (advert.get("street") or "").strip()
    number = (advert.get("houseNumber") or "").strip()
    district = (advert.get("cityDistrict") or "").strip()
    city = (advert.get("city") or "").strip()

    street_part = f"{street} {number}".strip()
    parts = [p for p in (street_part, district, city) if p]
    if parts:
        return ", ".join(parts)
    fallback = (advert.get("address") or "").strip()
    return fallback or None


def parse_advert(advert: dict, url: str) -> Optional[NormalizedListing]:
    source_id = advert.get("id")
    if source_id is None:
        return None

    property_type = ESTATE_TYPE_TO_PROPERTY.get(str(advert.get("estateType", "")).upper())
    transaction_type = OFFER_TYPE_TO_TRANSACTION.get(str(advert.get("offerType", "")).upper())
    if property_type is None or transaction_type is None:
        # Out of scope (land, garage, commercial) or unknown - skip quietly.
        return None

    lat, lon = _gps_pair(advert)
    description = advert.get("description") or advert.get("title")

    listing = NormalizedListing(
        source=SOURCE_NAME,
        source_id=str(source_id),
        url=url,
        property_type=property_type,
        transaction_type=transaction_type,
        disposition=normalize_disposition(advert.get("disposition")),
        area_m2=safe_float(advert.get("surface")),
        floor=safe_int(advert.get("etage")),
        lat=lat,
        lon=lon,
        address=_address_of(advert),
        description=description.strip() if isinstance(description, str) else None,
        price=safe_int(advert.get("price")),
    )
    listing.in_target_area = is_in_target_area(lat, lon)
    listing.priority_zone = is_priority_zone(lat, lon)
    return listing


def fetch_listing(session: requests.Session, url: str) -> tuple[Optional[NormalizedListing], Optional[dict]]:
    """Fetch one permitted detail page and parse it.

    Returns (listing, raw_advert_json). Either may be None - a page that
    fails to fetch or whose shape we do not recognise is reported by the
    caller, never raised.
    """
    try:
        html = net.fetch_text(session, url)
    except net.RequestFailed:
        return None, None
    advert = extract_advert_json(html)
    if advert is None:
        return None, None
    return parse_advert(advert, url), advert


def fetch_all(
    session: requests.Session,
    budget: Optional[Budget] = None,
    known_urls: Optional[Iterable[str]] = None,
    due_urls: Optional[Iterable[str]] = None,
    revisit_order: Optional[dict] = None,
    max_listings: Optional[int] = None,
    transactions: Optional[Iterable[str]] = None,
) -> tuple[list[NormalizedListing], list[dict], list[str], set[tuple[str, str]]]:
    """Collect listings via the sitemap + permitted detail pages.

    Unlike an API walk, every listing costs one request, so this is bounded
    by the run budget and by `max_listings`. Listings already known to the
    caller are visited first-come-last so fresh ones are not starved.

    Returns (normalized_listings, raw_pages, errors, completed_scopes).
    `completed_scopes` is empty unless the entire sitemap walk finished AND
    every selected listing was visited - absence-marking must not run on a
    partial sweep (see run.merge_source).
    """
    normalized: list[NormalizedListing] = []
    raw_pages: list[dict] = []
    errors: list[str] = []

    urls, sitemap_errors = iter_sitemap_listing_urls(session, budget)
    errors.extend(sitemap_errors)
    # How much of the sitemap this walk actually saw is the number that
    # explains a low listing count, so it belongs in the run log rather than
    # being silently invisible.
    raw_pages.append({
        "kind": "index",
        "sitemap_matched_urls": len(urls),
        "sitemap_errors": len(sitemap_errors),
    })
    if not urls:
        return normalized, raw_pages, errors, set()

    # Sale and rent are collected on different schedules, so a run takes only
    # the half it was asked for. The slug carries it - "…-nabidka-prodej-bytu-…"
    # - which means the filter costs nothing: a listing that is not wanted is
    # dropped from the sitemap list without ever being fetched.
    if transactions:
        wanted = set(transactions)
        urls = [u for u in urls if (meta_from_url(u) or (None, None, None))[2] in wanted]
        if not urls:
            errors.append(
                f"bezrealitky: the sitemap held no {'/'.join(sorted(wanted))} listings"
            )
            return normalized, raw_pages, errors, set()

    known = set(known_urls or ())
    due = set(due_urls or ())
    fresh = [u for u in urls if u not in known]
    # Every known listing is due every run; the budget, not a schedule,
    # decides how far the run gets. So the queue is ordered
    # least-recently-seen first: without that, a run able to cover two thirds
    # of Prague would cover the *same* two thirds every hour and never reach
    # the rest.
    revisit = [u for u in urls if u in known and u in due]
    if revisit_order:
        revisit.sort(key=lambda u: revisit_order.get(u, ""))
    # Within the fresh set, the watched area goes first: a new listing on
    # Nurmiho should not wait behind four hundred listings in Modrany. The
    # slug is all there is to go on - a listing is only a sitemap URL until
    # it has been fetched.
    fresh.sort(key=lambda u: 0 if collection_area.name_suggests_area(u) else 1)
    ordered = fresh + revisit

    limit = max_listings if max_listings is not None else len(ordered)
    selected = ordered[:limit]

    fetched_urls: set[str] = set()
    for url in selected:
        if budget is not None and budget.time_exhausted():
            errors.append(interruptions.interruption(
                "bezrealitky: listing sweep stopped - run time budget exhausted"
            ))
            break
        net.polite_sleep()
        fetched_urls.add(url)
        listing, advert = fetch_listing(session, url)
        if advert is not None:
            raw_pages.append({
                "kind": "detail",
                "url": url,
                # So run.py can tell whose payload this is when deciding
                # whether it is worth archiving at all.
                "source_id": (listing.source_id if listing is not None else None),
                "response": advert,
            })
        if listing is None:
            continue
        normalized.append(listing)

    # Everything else the sitemap listed is still published - that is what a
    # sitemap *is*. Emitting a presence-only sighting for each keeps those
    # listings alive without a request, which is what makes a tiered refresh
    # schedule compatible with reliable removal detection: a listing is only
    # ever marked missing when the site itself stopped publishing it, never
    # merely because this run chose not to re-read it.
    for url in urls:
        if url in fetched_urls:
            continue
        presence = presence_listing(url)
        if presence is not None:
            normalized.append(presence)

    # The sitemap walk - not the page sweep - is what proves coverage, so a
    # budget-truncated page sweep does not invalidate absence-marking.
    completed_scopes: set[tuple[str, str]] = set()
    if not sitemap_errors:
        completed_scopes = {
            (listing.property_type, listing.transaction_type) for listing in normalized
        }

    return normalized, raw_pages, errors, completed_scopes
