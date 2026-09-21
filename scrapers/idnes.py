"""Reality.iDNES.cz - flats in Prague.

## What its rules say

`robots.txt` is `User-agent: * / Allow: /` with a list of disallowed *search
filter* URL shapes (multi-value locality, sort combinations, the broker
directory's paging). Plain `?page=N` on a search result is not among them,
and detail pages are not restricted at all. Its VOP, fetched and read in
docs/robots/reality.idnes.cz.terms-1.*.pdf, are advertiser billing terms and
say nothing about collecting data. So this scraper walks the route the site
offers, like bezrealitky and unlike sreality - no override, and none needed.

## What it collects, and why that shape

Flats only, Prague only, both sale and rent - the brief asks for exactly
that from this source.

The important discovery is that the search result card already carries
almost everything:

    <h2 class="c-products__title">prodej bytu 1+kk 38 m²</h2>
    <p class="c-products__info">Univerzitní, Praha 10 - Malešice</p>
    <p class="c-products__price"><strong>7 752 704 Kč</strong></p>
    <a href=".../detail/prodej/byt/praha-10-univerzitni/6a99...d8/">

Transaction, property type, disposition, area, street, district and price,
all from one index page covering 26 listings. That makes a complete pass over
Prague's ~4050 flats for sale and ~2635 for rent cost about 257 requests
rather than 6685, and it is why detail pages are fetched only where the extra
field they add - the description - is actually wanted.

## The thing iDNES does not publish: coordinates

There is no schema.org block, no data-lat, no GPS anywhere in a detail page;
the map is loaded separately. Every other source in this project is placed
geographically, and this one cannot be. So:

  - listings are placed by their address string ("Univerzitní, Praha 10 -
    Malešice"), which the card gives for free;
  - a geographic rotation is impossible here for the same reason, so the
    once-a-day full pass comes from rotating through the index pages with a
    persistent cursor instead. That delivers the same guarantee - everything
    seen within 24 hours - without pretending to a precision this source does
    not offer. (A 24-sector grid over Prague was built for this and then
    removed: iDNES has no coordinates to place a listing in a sector, and no
    other source needed it.)

## How a run divides its attention

Three things happen every hour, in this order:

  1. the newest pages of each transaction type, because iDNES sorts newest
     first and a new listing is the thing most worth knowing about;
  2. every known listing in the watched area, re-read in full;
  3. as much of the rest of the index as the budget allows, continuing from
     where the last run stopped (common/progress.py).

Step 3 is the part that is not expected to finish, and does not need to.
"""

from __future__ import annotations

import html as html_module
import re
from typing import Iterable, Optional

import requests

from common import collection_area, net, interruptions
from common.budget import Budget
from common.geo import is_priority_zone
from common.schema import NormalizedListing, safe_float, safe_int

SOURCE_NAME = "idnes"

BASE = "https://reality.idnes.cz"
# Flats only, Prague only. Both are part of the URL rather than a query
# parameter, which keeps us clear of every pattern robots.txt disallows.
SEARCH_URLS = {
    ("byt", "prodej"): f"{BASE}/s/prodej/byty/praha/",
    ("byt", "pronajem"): f"{BASE}/s/pronajem/byty/praha/",
}

# The watched area, as the branches of the search tree that hold it.
#
# iDNES is the one source with no geography in its data at all - it publishes
# no coordinates - so the only way to ask it for part of Prague is to ask a
# narrower branch. Probed on 2026-09-21 (tools/probe_idnes_scope.py):
#
#   /s/prodej/byty/praha-4/    -> praha-4, praha-12, praha-11
#   /s/prodej/byty/praha-10/   -> praha-10, praha-15, praha-22
#   /s/prodej/byty/praha-11/   -> praha-11 (96%)
#   anything finer (katastr, street, query filter) -> 404
#
# These are POSTAL districts, so each branch carries the neighbouring city
# districts that share its post code - which is why two branches are enough:
# the watched area's iDNES listings sit in praha-11 (34%), praha-15 (31%),
# praha-10 (17%) and praha-4 (12%), and praha-4 covers 11 while praha-10
# covers 15. 572 of 605, or 94.5%.
#
# The remaining 5.5% are listings whose zone was decided by coordinates
# rather than by address, and they are not lost - the nightly city-wide pass
# picks them up. That division is the whole design: the narrow branches keep
# the hourly pass small, and the nightly pass is what keeps them honest.
AREA_BRANCHES = ("praha-4", "praha-10")


def search_url(property_type: str, transaction_type: str,
               branch: str = "") -> str:
    """The search root for a scope, optionally narrowed to one branch."""
    base = SEARCH_URLS[(property_type, transaction_type)]
    if not branch:
        return base
    # ".../byty/praha/" -> ".../byty/praha-10/"
    return base.rsplit("praha/", 1)[0] + f"{branch}/"

# Listing links are the only reliable anchor in the markup: a 24-character
# hex id at the end of a /detail/<transaction>/<type>/<locality>/ path.
LISTING_HREF_RE = re.compile(
    r'href="(https://reality\.idnes\.cz/detail/(prodej|pronajem)/(byt|dum)/[^"/]+/([0-9a-f]{24})/)"'
)

# The card's three fields, each anchored on its own class name. Matched
# non-greedily and independently so that a change to one does not silently
# take the others with it.
TITLE_RE = re.compile(r'<h2 class="c-products__title">(.*?)</h2>', re.S)
INFO_RE = re.compile(r'<p class="c-products__info">(.*?)</p>', re.S)
PRICE_RE = re.compile(r'<p class="c-products__price">.*?<strong>(.*?)</strong>', re.S)

# "prodej bytu 1+kk 38 m²" / "pronájem bytu 2+1 55 m²" - the disposition and
# the area, from the one string that always carries both.
TITLE_PARSE_RE = re.compile(
    r"(?:bytu|domu)\s+(?P<disposition>[0-9]\s*\+\s*(?:kk|[0-9])|garsoniera|atypick[yý])?\s*"
    r"(?P<area>[0-9][0-9\s]*)\s*m",
    re.I,
)

# How many pages of newest-first results to read every hour regardless of the
# rotation. Three pages is 78 listings per transaction type; iDNES reports
# roughly 589 new flats for sale a week, so an hour's worth of new stock is
# nowhere near that - the margin is for a burst.
NEWEST_PAGES_PER_RUN = 3

# How many watched-area listings one run may re-read. At one request a second
# this is a two-minute slice of the hour; the rest of the watched area comes
# up in the next run, oldest first.
MAX_WATCHED_PER_RUN = 150

# When "some detail pages do not parse" becomes "this scraper is broken".
UNPARSED_ALARM_MIN = 10
UNPARSED_ALARM_RATIO = 0.5

MAX_PAGES_SAFETY = 400


def _text(markup: str) -> str:
    """Tags stripped, entities unescaped, whitespace collapsed.

    html.unescape rather than a hand-written table of the common entities:
    the first version used a table and missed the *numeric* ones, so
    "7 752 704 K&#269;" - a price followed by an escaped c-hacek - had the
    269 appended to it and became 7,752,704,269 Kc. Every entity, or the
    digits inside the ones that were missed end up in the numbers.
    """
    text = re.sub(r"<[^>]+>", " ", markup)
    return re.sub(r"\s+", " ", html_module.unescape(text)).strip()


def parse_price(raw: str) -> Optional[int]:
    """"7 752 704 Kč" -> 7752704. "Info o ceně u RK" -> None.

    A listing whose price is hidden behind "ask the agent" is still a real
    listing worth tracking; it simply has no price yet, and inventing a zero
    for it would poison every average this dataset is for.
    """
    digits = re.sub(r"[^\d]", "", _text(raw))
    return int(digits) if digits else None


def parse_title(title: str) -> dict:
    """Disposition and area out of "prodej bytu 1+kk 38 m²"."""
    text = _text(title)
    match = TITLE_PARSE_RE.search(text)
    if not match:
        return {"disposition": None, "area_m2": None}
    disposition = match.group("disposition")
    if disposition:
        disposition = re.sub(r"\s+", "", disposition).lower()
        if disposition.startswith("garsoniera"):
            disposition = "1+kk"  # same normalisation bezrealitky gets
    return {
        "disposition": disposition,
        "area_m2": safe_float(re.sub(r"\s+", "", match.group("area"))),
    }


def parse_card(block: str) -> Optional[NormalizedListing]:
    """One search-result card into a listing, or None if it is not one."""
    href_match = LISTING_HREF_RE.search(block)
    if href_match is None:
        return None
    url, transaction, estate, source_id = href_match.groups()
    if estate != "byt":
        # Flats only, per the brief - iDNES also lists houses on other pages.
        return None

    title_match = TITLE_RE.search(block)
    info_match = INFO_RE.search(block)
    price_match = PRICE_RE.search(block)

    parsed = parse_title(title_match.group(1) if title_match else "")
    address = _text(info_match.group(1)) if info_match else None

    listing = NormalizedListing(
        source=SOURCE_NAME,
        source_id=source_id,
        url=url,
        property_type="byt",
        transaction_type=transaction,
        disposition=parsed["disposition"],
        area_m2=parsed["area_m2"],
        price=parse_price(price_match.group(1)) if price_match else None,
        address=address,
    )
    # No coordinates from this source at all (see the module docstring), so
    # geography comes from the address text and nothing pretends otherwise.
    listing.in_target_area = True  # the search URL is already Prague-only
    listing.priority_zone = collection_area.name_suggests_area(address)
    listing.extra["placed_by"] = "address"
    return listing


def parse_search_page(html: str) -> list:
    """Every listing on one search results page.

    Split on the card wrapper rather than matched with one big regex: cards
    that fail to parse should be skipped individually, not take the page with
    them.
    """
    listings = []
    for block in html.split('<div class="c-products__item">')[1:]:
        try:
            listing = parse_card(block)
        except (ValueError, AttributeError):
            continue  # one malformed card never costs the page
        if listing is not None:
            listings.append(listing)
    return listings


def page_url(property_type: str, transaction_type: str, page: int,
             branch: str = "") -> str:
    base = search_url(property_type, transaction_type, branch)
    return base if page <= 1 else f"{base}?page={page}"


class EndOfIndex(Exception):
    """Past the last page of a search.

    iDNES answers 404 there rather than serving an empty page, so the end of
    the index arrives as what looks exactly like a broken request. Treating it
    as one cost more than a spurious error in the log: the walk only marks a
    scope complete when it reaches the end, and a scope that is never complete
    is never absence-marked - so no iDNES listing could ever have been found to
    have gone. Seen in the first rental run, at page 114.
    """


def fetch_search_page(session, property_type: str, transaction_type: str,
                      page: int, branch: str = ""):
    """(listings, raw page record, error). Never raises except EndOfIndex."""
    url = page_url(property_type, transaction_type, page, branch)
    try:
        html = net.fetch_text(session, url)
    except net.RequestFailed as exc:
        if getattr(exc, "status", None) == 404 and page > 1:
            # Only past the first page. A 404 on page one is the search URL
            # itself having changed, which is a real failure and must stay one.
            raise EndOfIndex(url) from exc
        return [], None, f"idnes {transaction_type} page={page}: {exc}"
    listings = parse_search_page(html)
    return listings, {"kind": "index", "url": url, "listings": len(listings)}, None


def total_pages(html: str) -> Optional[int]:
    """Highest page number the pagination offers, if it says."""
    pages = [int(n) for n in re.findall(r"[?&]page=(\d+)", html)]
    return max(pages) if pages else None


OG_DESCRIPTION_RE = re.compile(r'<meta property="og:description" content="([^"]*)"')

# "Prodej bytu 2+kk 65 m², Ceskoslovenskeho exilu, Praha 4 - Modrany. Cena
#  23 600 000 Kc. Nabizi realitni kancelar PSN s.r.o.. <the description>"
#
# One string that happens to carry every field this project wants, which is
# why a detail page needs no HTML parsing at all - and why it survives iDNES
# redesigning its markup, since og:description is what their own social
# previews depend on.
OG_PARSE_RE = re.compile(
    r"^(?P<transaction>Prodej|Pron[aá]jem)\s+(?P<estate>bytu|domu)\s+"
    r"(?P<disposition>[^\s,]+)\s+(?P<area>[\d\s]+)\s*m[²2],\s*"
    r"(?P<rest>.*)$",
    re.I | re.S,
)

PRICE_SENTENCE_RE = re.compile(r"^Cena\s+(?P<price>[\d\s]+)\s*K[čc]\.?", re.I)

# Periods that do not end the address. Czech streets are named after people,
# and people have initials.
ADDRESS_ABBREVIATIONS = frozenset(
    "nam nám tr tř ul sv gen dr ing mgr mudr judr st plk pplk kpt por prof "
    "akad bratri bratří".split()
)


def split_address(rest: str) -> Optional[tuple[str, str]]:
    r"""The address, and whatever follows it.

    This used to be `[^.]+\.` - the address ran to the first period. That is
    right until a street is named after somebody with initials: "R.A.
    Dvorskeho, Praha 10 - Horni Mecholupy" parsed as the address "R", and
    two real listings on R.A. Dvorskeho lost their location that way on the
    first run that fetched their detail pages.

    The period that ends the address is the one that is not part of an
    initial ("R.", "A.") or of the handful of abbreviations street names use
    ("nam.", "sv.", "Dr."). Returns None when there is no such period, which
    means the text is not in the shape this parser understands.
    """
    for match in re.finditer(r"\.", rest):
        before = rest[:match.start()]
        token = re.split(r"[\s,.]", before)[-1]
        if len(token) == 1 and token.isalpha():
            continue
        if token.lower() in ADDRESS_ABBREVIATIONS:
            continue
        return before.strip(), rest[match.end():].lstrip()
    return None

TRANSACTION_WORDS = {"prodej": "prodej", "pronájem": "pronajem", "pronajem": "pronajem"}
ESTATE_WORDS = {"bytu": "byt", "domu": "dum"}


def parse_og_description(text: str, url: str) -> Optional[NormalizedListing]:
    """A whole listing out of the one meta tag iDNES fills in for link previews."""
    href_match = re.search(r"/detail/(prodej|pronajem)/(byt|dum)/[^/]+/([0-9a-f]{24})/", url)
    if href_match is None:
        return None
    match = OG_PARSE_RE.match(text.strip())
    if match is None:
        return None
    estate = ESTATE_WORDS.get(match.group("estate").lower())
    if estate != "byt":
        return None

    split = split_address(match.group("rest"))
    if split is None:
        return None
    address, tail = split

    price_match = PRICE_SENTENCE_RE.match(tail)
    price = (safe_int(re.sub(r"\s+", "", price_match.group("price")))
             if price_match else None)
    description = tail[price_match.end():].lstrip(" .") if price_match else tail
    # "Nabizi realitni kancelar X s.r.o.." is boilerplate about the agency,
    # not about the flat, and it repeats on thousands of listings. Stopping at
    # the first full stop does not work - agency names are full of them
    # ("s.r.o.") - so this runs to the first sentence end that is followed by
    # the start of a new sentence.
    description = re.sub(
        r"^Nab[ií]z[ií].*?\.\.?\s+(?=[A-ZÁ-Žа-я0-9])", "", description, flags=re.S
    ).strip() or None

    listing = NormalizedListing(
        source=SOURCE_NAME,
        source_id=href_match.group(3),
        url=url,
        property_type="byt",
        transaction_type=TRANSACTION_WORDS.get(match.group("transaction").lower(), "prodej"),
        disposition=match.group("disposition").lower(),
        area_m2=safe_float(re.sub(r"\s+", "", match.group("area"))),
        price=price,
        address=address,
        description=description,
    )
    listing.in_target_area = True
    listing.priority_zone = collection_area.name_suggests_area(address)
    listing.extra["placed_by"] = "address"
    return listing


def fetch_detail(session, url: str):
    """One listing page, parsed from its og:description meta tag.

    Returns (listing, error). A page that loads fine but carries no usable
    og:description is NOT an error: iDNES renders some listings - developer
    projects, mainly - from a different template, and those pages return a
    perfectly good HTTP 200 with different markup. There are real ones of
    these in the watched area right now, on Honzikova and U zakrutu.

    Treating each as an error turned every run red from the moment the second
    one was discovered, and the count grew hour by hour as more watched-area
    listings accumulated. Nothing is actually lost when this happens: the
    search card already supplied price, area, disposition and address, and
    the description is the only field this request was for.

    The case that *is* worth an error - iDNES changing its markup, so that no
    page parses any more - is caught by the caller, which looks at the
    proportion rather than at any single page.
    """
    try:
        html = net.fetch_text(session, url)
    except net.RequestFailed as exc:
        return None, f"idnes detail {url}: {exc}"
    match = OG_DESCRIPTION_RE.search(html)
    if match is None:
        return None, None
    return parse_og_description(html_module.unescape(match.group(1)), url), None



def _transaction_of(url: Optional[str]) -> Optional[str]:
    """prodej / pronajem from a listing URL, for filtering known listings."""
    match = re.search(r"/detail/(prodej|pronajem)/", url or "")
    return match.group(1) if match else None


def fetch_all(
    session: requests.Session,
    budget: Optional[Budget] = None,
    known: Optional[dict] = None,
    page_cursor: Optional[dict] = None,
    max_pages: Optional[int] = None,
    max_watched: int = MAX_WATCHED_PER_RUN,
    transactions: Optional[Iterable[str]] = None,
    branches: Optional[Iterable[str]] = None,
):
    """One run's worth of iDNES, in priority order.

    `known` maps source_id -> stored row, used to decide which listings are
    already placed in the watched area and therefore due a full re-read.
    `page_cursor` maps "byt/prodej" -> the page the last run stopped at, and
    the same shape comes back out to be stored for the next one.

    Returns (listings, raw_pages, errors, completed_scopes, pages_consumed,
    page_cursors).
    `completed_scopes` is non-empty only when a whole transaction type was
    walked to its end this run - absence must never be inferred from a
    rotation that only saw a twelfth of the city.
    """
    wanted = set(transactions) if transactions else {t for _, t in SEARCH_URLS}
    # (property type, transaction, branch). One branch per scope, and the
    # empty branch is the whole city - which is what every caller got before
    # branches existed and what the nightly pass still asks for.
    #
    # The branch is part of the scope, not a setting around it, because the
    # page cursor is keyed per scope: a city walk and an area walk that
    # shared a cursor would each resume where the other stopped, and both
    # would walk a fraction of what they meant to.
    branch_list = tuple(branches) if branches else ("",)
    # Absence marking asks "was this scope walked to the end?" and then treats
    # everything it did not see as gone. A branch walk reaches the end of a
    # BRANCH, which is a different and much smaller population - so on an area
    # pass no scope may ever be reported complete, or every listing outside
    # Praha 4 and Praha 10 would be marked missing within the hour.
    walked_whole_city = branch_list == ("",)
    scopes = [(ptype, transaction, branch)
              for ptype, transaction in SEARCH_URLS if transaction in wanted
              for branch in branch_list]
    known = known or {}
    listings: list = []
    raw_pages: list = []
    errors: list = []
    completed: set = set()
    pages_consumed = 0
    seen_ids: set = set()

    def take(batch):
        for listing in batch:
            if listing.source_id in seen_ids:
                continue
            seen_ids.add(listing.source_id)
            listings.append(listing)

    # --- 1. the newest listings, every run ------------------------------
    # iDNES sorts newest first, so the front of the list is where anything
    # that appeared in the last hour will be. This is the part of the run
    # that must never be skipped, whatever the budget does afterwards.
    for scope in scopes:
        for page in range(1, NEWEST_PAGES_PER_RUN + 1):
            if budget is not None and budget.time_exhausted():
                errors.append(interruptions.interruption(
                    "idnes: stopped during newest-first pages - time budget exhausted"))
                break
            net.polite_sleep()
            try:
                batch, raw, error = fetch_search_page(session, scope[0], scope[1], page,
                                                      scope[2])
            except EndOfIndex:
                # Fewer pages than NEWEST_PAGES_PER_RUN in this category -
                # a small one, not a fault. Nothing more to read here.
                pages_consumed += 1
                break
            pages_consumed += 1
            if error:
                errors.append(error)
                continue
            if raw is not None:
                raw_pages.append(raw)
            take(batch)

    # --- 2. everything known to be in the watched area, every run -------
    # Placed by address, because this source publishes no coordinates. These
    # are re-read from their own pages so a price change in Hostivar or on
    # Roztylske namesti is caught within the hour rather than within a day.
    # Least-recently-seen first, and capped. Unbounded, this loop is one
    # request per watched listing before the rotation gets a single page -
    # so a few hundred listings in Hostivar would quietly consume the hour
    # that the rest of Prague is supposed to share. Capped and ordered, a
    # busy watched area rotates through itself over a few hours instead of
    # starving everything behind it.
    watched = sorted(
        (row for row in known.values()
         if collection_area.name_suggests_area(row.get("address"))
         and _transaction_of(row.get("url")) in wanted),
        key=lambda row: row.get("last_seen_at") or "",
    )[:max_watched]
    attempted = unparsed = 0
    for row in watched:
        if budget is not None and budget.time_exhausted():
            errors.append(interruptions.interruption(
                "idnes: stopped during watched-area refresh - time budget exhausted"))
            break
        net.polite_sleep()
        detail, error = fetch_detail(session, row["url"])
        pages_consumed += 1
        attempted += 1
        if error:
            errors.append(error)
            continue
        if detail is None:
            # A page that loaded but did not parse. Normal in small numbers -
            # see fetch_detail - and only meaningful in bulk, below.
            unparsed += 1
            continue
        raw_pages.append({"kind": "detail", "url": row["url"], "source_id": row["source_id"]})
        if detail is not None:
            # Ahead of anything the index produced for the same listing: this
            # one carries the description as well, and it was fetched
            # precisely because this listing is in the watched area.
            seen_ids.discard(detail.source_id)
            listings[:] = [l for l in listings if l.source_id != detail.source_id]
            take([detail])

    # One unparseable detail page is a listing rendered from another
    # template; most of them unparseable is iDNES having changed its markup,
    # which is a real failure and has to be said out loud rather than
    # absorbed silently. The threshold needs a floor as well as a ratio, or
    # two odd listings out of three would raise a false alarm.
    if attempted >= UNPARSED_ALARM_MIN and unparsed > attempted * UNPARSED_ALARM_RATIO:
        errors.append(
            f"idnes: {unparsed} of {attempted} detail pages carried no usable "
            "og:description - the markup has most likely changed"
        )

    # --- 3. the rest of the index, continuing from last time ------------
    # A full pass over Prague's flats is about 257 pages. A run takes as many
    # as it has time for and records where it stopped; the next one picks up
    # exactly there. Nothing here needs to finish, which is the point.
    #
    # Each transaction type keeps its own page cursor. Running off the end is
    # how the end is discovered - iDNES does not say how many pages there are
    # - and it is also what proves a full pass happened, so the scope is only
    # marked complete when an empty page is actually reached.
    per_scope = max(1, (max_pages or MAX_PAGES_SAFETY) // max(1, len(scopes)))
    new_cursors = dict(page_cursor) if isinstance(page_cursor, dict) else {}
    for scope in scopes:
        key = f"{scope[0]}/{scope[1]}" + (f"@{scope[2]}" if scope[2] else "")
        page = max(1, int(new_cursors.get(key, 1) or 1))
        walked = 0
        while walked < per_scope and page <= MAX_PAGES_SAFETY:
            if budget is not None and budget.time_exhausted():
                errors.append(interruptions.interruption(
                    f"idnes {key}: index rotation stopped at page {page} - time budget exhausted"))
                break
            net.polite_sleep()
            try:
                batch, raw, error = fetch_search_page(session, scope[0], scope[1], page,
                                                      scope[2])
            except EndOfIndex:
                # The same thing an empty page means, said with a status code.
                pages_consumed += 1
                if walked_whole_city:
                    completed.add(scope[:2])
                page = 1
                break
            pages_consumed += 1
            walked += 1
            if error:
                errors.append(error)
                page += 1
                break
            if raw is not None:
                raw_pages.append(raw)
            if not batch:
                if walked_whole_city:
                    completed.add(scope[:2])
                page = 1  # wrap: the next run starts the pass again
                break
            take(batch)
            page += 1
        new_cursors[key] = page

    return listings, raw_pages, errors, completed, pages_consumed, new_cursors
