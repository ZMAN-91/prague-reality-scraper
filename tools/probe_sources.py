"""Refresh the facts this project rests on, from the live sites.

Not a data collector. It fetches four things and writes them into docs/,
where they are committed and therefore diffable:

  - each portal's robots.txt, verbatim - including the three portals this
    project deliberately does NOT collect from, because those snapshots are
    the evidence for why, and tests/test_robots_policy.py reads them;
  - the terms of use each portal publishes, following the links the site
    itself offers and the PDFs behind them, since that is how both
    bezrealitky and iDNES publish their contracts;
  - the reference coordinates common/collection_area.py is built from,
    resolved against a real service rather than remembered - an earlier
    hand-written "Sporilov" sat 1.9 km from Sporilov;
  - one page of real iDNES search markup, which tests/test_idnes.py parses,
    because a parser tested against invented HTML only proves that the
    invention matches the parser.

    python -m tools.probe_sources

Read-only, polite, and never part of a scheduled scrape. Run it by hand
(or via .github/workflows/probe.yml) when a portal appears to have changed
something; whatever changed then shows up as a diff in docs/.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.robotparser
from urllib.parse import urljoin
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

from common import net

TIMEOUT = 20

# The four portals added after sreality and bezrealitky. Each is probed the
# same way the first two were: robots.txt verbatim, then its own terms, then
# whatever its search page reveals about how it serves data. Which of the two
# existing patterns each one gets - bezrealitky's "walk the route the site
# offers" or sreality's "there is no permitted route at all" - is a decision
# for after the text is read, not before.
# Every host whose rules this project relies on - including the three it does
# NOT collect from. Their robots.txt is the evidence for why they are left
# alone (docs/podminky.md), and tests/test_robots_policy.py reads these
# snapshots, so a portal loosening or tightening its rules shows up as a diff
# rather than as a decision nobody revisited.
PORTALS = [
    ("https://www.sreality.cz",
     ["/api/v1/estates/search", "/hledani/prodej/byty/praha"]),
    ("https://www.bezrealitky.cz",
     ["/nemovitosti-byty-domy/1-x", "/sitemap/sitemap.xml", "/vyhledat"]),
    ("https://api.bezrealitky.cz", ["/graphql/"]),
    ("https://reality.idnes.cz",
     ["/s/prodej/byty/praha/", "/s/prodej/byty/praha/?page=2", "/detail/prodej/byt/praha/abc/"]),
    # Not collected: realingo forbids crawlers in its terms, CeskeReality's
    # robots.txt is "Disallow: /", and Bazos was dropped. Watched anyway, so
    # the reasons stay checkable.
    ("https://www.realingo.cz", ["/praha"]),
    ("https://www.ceskereality.cz", ["/prodej/byty/praha/"]),
    ("https://reality.bazos.cz", ["/prodam/byt/"]),
]

# Search-result pages to look at for each new portal: where a listing URL
# shape, an embedded JSON blob or an XHR endpoint would show up.
# Places whose coordinates the collection area is built from. Guessing these
# from memory is how a scraper ends up watching the wrong square kilometre -
# an earlier version put "sporilov" about 1.5 km from Sporilov - so they are
# resolved once against OpenStreetMap's Nominatim and written into the repo
# with their provenance. Nominatim's usage policy allows this: a handful of
# requests, one per second, from a script that identifies itself honestly.
REFERENCE_PLACES = [
    "Roztylske namesti, Praha, Czechia",
    "Sporilov, Praha, Czechia",
    "Horni Mecholupy, Praha, Czechia",
    "Dolni Mecholupy, Praha, Czechia",
    "Hostivar, Praha, Czechia",
    "Hostivarsky lesopark, Praha, Czechia",
    "Hostivarska prehrada, Praha, Czechia",
    "Zabehlice, Praha, Czechia",
    "Zahradni Mesto, Praha, Czechia",
    "Roztyly, Praha, Czechia",
    "Chodov, Praha 11, Czechia",
    "Petrovice, Praha, Czechia",
    "Michle, Praha, Czechia",
    "Krc, Praha, Czechia",
    "Strasnice, Praha, Czechia",
    "Nurmiho, Praha, Czechia",
    "Trebesin, Praha, Czechia",
    "Sterboholy, Praha, Czechia",
]

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Czech Wikipedia article titles for the same places, used when Nominatim is
# unavailable or its robots.txt says no. Not every place has an article - a
# square usually does not - and the ones that are missing simply stay missing
# rather than being invented.
WIKIPEDIA_FALLBACK = {
    "Sporilov, Praha, Czechia": "Spořilov",
    "Horni Mecholupy, Praha, Czechia": "Horní Měcholupy",
    "Dolni Mecholupy, Praha, Czechia": "Dolní Měcholupy",
    "Hostivar, Praha, Czechia": "Hostivař",
    "Hostivarsky lesopark, Praha, Czechia": "Hostivařský lesopark",
    "Hostivarska prehrada, Praha, Czechia": "Hostivařská přehrada",
    "Zabehlice, Praha, Czechia": "Záběhlice",
    "Zahradni Mesto, Praha, Czechia": "Zahradní Město",
    "Roztyly, Praha, Czechia": "Roztyly",
    "Chodov, Praha 11, Czechia": "Chodov (Praha)",
    "Petrovice, Praha, Czechia": "Petrovice (Praha)",
    "Michle, Praha, Czechia": "Michle",
    "Krc, Praha, Czechia": "Krč",
    "Strasnice, Praha, Czechia": "Strašnice",
    "Trebesin, Praha, Czechia": "Třebešín",
    "Sterboholy, Praha, Czechia": "Štěrboholy",
    # These four came back empty on the first run and are exactly the ones
    # the brief singles out, so they get their own entries rather than being
    # quietly approximated from a neighbour.
    "Roztylske namesti, Praha, Czechia": "Roztylské náměstí",
    "Hostivarsky lesopark, Praha, Czechia": "Hostivařský lesopark",
    "Hostivarska prehrada, Praha, Czechia": "Hostivařská přehrada",
    "Roztyly, Praha, Czechia": "Roztyly",
}

# A deliberately trivial query: we only want to learn whether the endpoint
# speaks GraphQL at all and what it calls its types, not to extract data.
def hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def probe_robots(
    session: requests.Session,
    origin: str,
    paths: list[str],
    save_dir: "Path | None" = None,
) -> None:
    hr(f"robots.txt — {origin}")
    try:
        resp = session.get(f"{origin}/robots.txt", timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"  could not fetch: {exc}")
        return
    print(f"  HTTP {resp.status_code}, {len(resp.text)} bytes")
    if resp.status_code != 200:
        return

    lines = resp.text.splitlines()

    # The complete file, verbatim. Whether to scrape a site whose robots.txt
    # disallows the path you need is the operator's decision, not the
    # scraper's - and nobody can make that decision from a summary. So the
    # exact text goes in the log, line-numbered, in full...
    print(f"  --- FULL robots.txt VERBATIM ({len(lines)} lines) ---")
    for number, line in enumerate(lines, 1):
        print(f"    {number:4} | {line}")
    print("  --- end of robots.txt ---")

    # ...and, more usefully, into the repository, where it can be read at
    # leisure and diffed the next time it changes. A portal quietly
    # loosening or tightening its rules is exactly the kind of thing a
    # multi-year project wants in version control.
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        host = origin.split("//", 1)[-1]
        target = save_dir / f"{host}.robots.txt"
        header = (
            f"# Fetched {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
            f"from {origin}/robots.txt by tools/probe_sources.py\n"
            f"# Verbatim copy, unedited. {len(lines)} lines, {len(resp.text)} bytes.\n"
            "# ---------------------------------------------------------------\n"
        )
        target.write_text(header + resp.text, encoding="utf-8")
        print(f"  saved verbatim copy -> {target}")

    rp = urllib.robotparser.RobotFileParser()
    rp.parse(lines)
    print("  --- can_fetch() verdicts for this project's User-Agent ---")
    for path in paths:
        url = origin + path
        print(f"    {'ALLOWED' if rp.can_fetch(net.USER_AGENT, url) else 'DISALLOWED'}  {url}")
    print("  --- same, for a plain browser UA (for comparison) ---")
    browser = "Mozilla/5.0"
    for path in paths:
        url = origin + path
        print(f"    {'ALLOWED' if rp.can_fetch(browser, url) else 'DISALLOWED'}  {url}")



TERMS_KEYWORDS = (
    "podminky", "podmínky", "terms", "smluvni", "smluvní",
    "pravni", "právní", "vop", "pravidla",
)

# Third-party policy pages that a homepage links for its own embedded widgets
# (reCAPTCHA, analytics). They are not the portal's terms and following them
# only wastes the budget - bezrealitky's homepage linked policies.google.com
# as "smluvní podmínky" on the first run.
THIRD_PARTY_POLICY_HOSTS = ("google.com", "facebook.com", "instagram.com", "apple.com")


def _terms_url_strings(html: str, base: str) -> "list[tuple[str, str]]":
    """Terms-like URLs that appear as bare strings rather than <a href>.

    bezrealitky is a Next.js site: its terms index lists the actual contracts
    as client-side routes inside the __NEXT_DATA__ blob, so an anchor-only
    scan finds the page's footer and nothing else. The paths are still in the
    HTML, just not as markup.
    """
    found: list[tuple[str, str]] = []
    for path in re.findall(r"[\"'](/[A-Za-z0-9/_.\-]*(?:podminky|podm%C3%ADnky)[A-Za-z0-9/_.\-]*)[\"']", html, re.I):
        full = urljoin(base, path)
        if full not in [c[0] for c in found]:
            found.append((full, "(url found in page data, not as a link)"))
    return found


def _terms_links(html: str, base: str) -> "list[tuple[str, str]]":
    """Every terms-like anchor on a page, as (absolute url, link label)."""
    found: list[tuple[str, str]] = []
    for href, text in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
        label = re.sub(r"<[^>]+>", "", text).strip().lower()
        blob = (href + " " + label).lower()
        if not any(k in blob for k in TERMS_KEYWORDS):
            continue
        # urljoin, not string concatenation: the second level resolves hrefs
        # against a page URL like ".../informace/smluvni-podminky", where a
        # root-relative "/informace/obchodni-podminky" must drop the path
        # rather than be appended to it. Concatenation built 404s here and
        # that is why the real bezrealitky terms were missed on the first try.
        full = urljoin(base, href)
        if any(h in full for h in THIRD_PARTY_POLICY_HOSTS):
            continue
        if full not in [c[0] for c in found]:
            found.append((full, label))
    return found


# Phrases that only appear in an actual contract, not in a page that merely
# links to one. Counting numbered clauses was the obvious idea and it failed:
# the sreality terms are written as prose under headings, so a page full of
# real obligations scored zero.
CONTRACT_MARKERS = (
    "je zakázáno", "jste povinni", "vyhrazujeme si", "zavazujete se",
    "nesmí", "smluvní pokut", "oprávněni",
)


def _clause_density(text: str) -> int:
    """How many contract-like obligations the text contains.

    A portal's terms link usually leads to an index of the real documents,
    which is what both portals turned out to publish. This number is what
    distinguishes "a page listing six contracts" from the contract itself,
    so the log can say which saved file is worth reading.
    """
    lowered = text.lower()
    return sum(lowered.count(marker) for marker in CONTRACT_MARKERS)


def _registrable_domain(url: str) -> str:
    """The last two labels of a URL's host ("bezrealitky.cz").

    Both portals keep their contracts on a sibling host - sreality's on
    o-seznam.cz, bezrealitky's as PDFs on api.bezrealitky.cz - so matching on
    the exact host found the table of contents and stopped there.
    """
    host = url.split("//", 1)[-1].split("/", 1)[0].split(":")[0]
    return ".".join(host.split(".")[-2:])


def _extract_pdf_text(pdf_path: "Path", url: str, label: str, target: "Path") -> None:
    """Write a PDF's text next to it, so the contract is greppable and diffable.

    Kept best-effort and importing pypdf lazily: this is the only place in the
    project that needs a PDF library, and the scraper must never depend on one.
    A PDF whose fonts carry no ToUnicode map extracts as garbage, so the result
    is checked for real words before it is written - an unreadable file next to
    a contract invites the false conclusion that the contract says nothing.
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        print("    (pypdf not installed - PDF kept, text not extracted)")
        return
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf_path)).pages)
    except Exception as exc:  # any malformed PDF; this is a diagnostic, not a pipeline
        print(f"    could not extract text from {pdf_path.name}: {exc}")
        return
    words = len(re.findall(r"[A-Za-zÁ-Žá-ž]{4,}", text))
    if words < 50:
        print(f"    {pdf_path.name}: only {words} readable words - text not written")
        return
    txt_target = target.with_suffix(".pdf.txt")
    txt_target.write_text(
        f"# Text extracted from {url}\n"
        f"# Link label: {label!r}\n"
        f"# {words} readable words; extraction is best-effort - the PDF next to this\n"
        f"# file is the authoritative copy.\n"
        "# ---------------------------------------------------------------\n" + text,
        encoding="utf-8",
    )
    print(f"    text -> {txt_target}  ({words} readable words)")


def _save_terms(session: requests.Session, url: str, label: str, target: "Path") -> "str | None":
    """Fetch one terms document and save it. Returns HTML for further crawling.

    PDFs are stored as-is rather than stripped: bezrealitky publishes every
    one of its contracts as a PDF, and a PDF run through a tag stripper is
    noise. Nothing links onwards out of a PDF, so this returns None there and
    the crawl stops.
    """
    try:
        page = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"    could not fetch {url}: {exc}")
        return None
    if page.status_code != 200:
        print(f"    HTTP {page.status_code} for {url}")
        return None

    content_type = page.headers.get("Content-Type", "")
    if url.lower().endswith(".pdf") or "application/pdf" in content_type:
        pdf_target = target.with_suffix(".pdf")
        pdf_target.write_bytes(page.content)
        print(f"    saved -> {pdf_target}  ({len(page.content)} bytes, PDF: {label[:50]!r})")
        _extract_pdf_text(pdf_target, url, label, target)
        return None
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    clauses = _clause_density(text)
    target.write_text(
        f"# Fetched {datetime.now(timezone.utc).isoformat(timespec='seconds')} from {url}\n"
        f"# Link label: {label!r}\n"
        f"# Numbered clauses found: {clauses} "
        f"({'contract text' if clauses >= 5 else 'probably just an index of links'})\n"
        "# Tags stripped; wording unedited.\n"
        "# ---------------------------------------------------------------\n" + text,
        encoding="utf-8",
    )
    print(f"    saved -> {target}  ({clauses} numbered clauses)")
    return page.text


def find_terms_links(session: requests.Session, origin: str, save_dir: "Path | None" = None) -> None:
    """Locate and save each portal's terms of use, two levels deep.

    Whether a project like this is acceptable is not decided by robots.txt
    alone - the operator's actual terms matter, and they should be read
    rather than assumed. This finds the links a site itself publishes and
    stores the text next to the robots.txt snapshots, so the question can be
    answered from the real wording.

    Two levels, because one was not enough: both portals answer "smluvní
    podmínky" with a page that only lists the actual documents (sreality: a
    list of advertiser contracts by effective date; bezrealitky: a list of
    per-service terms). The clauses live one click further in.
    """
    hr(f"terms of use — {origin}")
    try:
        html = session.get(origin, timeout=TIMEOUT).text
    except requests.RequestException as exc:
        print(f"  could not fetch homepage: {exc}")
        return

    candidates = _terms_links(html, origin)
    if not candidates:
        print("  no terms-like links found on the homepage")
        return
    for url, label in candidates[:10]:
        print(f"    {label[:60]!r} -> {url}")

    if save_dir is None:
        return
    save_dir.mkdir(parents=True, exist_ok=True)
    host = origin.split("//", 1)[-1]
    seen = {origin}
    for index, (url, label) in enumerate(candidates[:4], 1):
        if url in seen:
            continue
        seen.add(url)
        page_html = _save_terms(session, url, label, save_dir / f"{host}.terms-{index}.txt")
        if page_html is None:
            continue
        # Second level: the documents this page points at - an index page is
        # a table of contents, not the contract. Matched on the registrable
        # domain, because both portals host their contracts on a sibling.
        page_domain = _registrable_domain(url)
        # Three runs found nothing below bezrealitky's terms index, so record
        # what the page actually links to instead of guessing at the filter a
        # fourth time. Saved rather than only printed: a job log scrolls away,
        # and "where do this portal's terms actually live" is worth keeping.
        all_anchors = re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page_html, re.S | re.I)
        dump = [f"# Every link on {url}", f"# {len(all_anchors)} anchors, fetched {datetime.now(timezone.utc).isoformat(timespec='seconds')}", ""]
        for href, text in all_anchors:
            label = re.sub(r"<[^>]+>", " ", text)
            label = re.sub(r"\s+", " ", label).strip()[:70]
            dump.append(f"{urljoin(url, href)}\t{label}")
        links_file = save_dir / f"{host}.terms-{index}.links.txt"
        links_file.write_text("\n".join(dump) + "\n", encoding="utf-8")
        print(f"    {len(all_anchors)} anchors -> {links_file}")

        # Any same-domain PDF linked from a terms page is a contract. This is
        # how both bezrealitky and Reality.iDNES.cz publish theirs, and the
        # iDNES ones are labelled only "stahnout PDF" with a filename of
        # vop_reality_idnes_cz_<date>.pdf - no keyword a filter could match.
        pdf_links = [
            (urljoin(url, href), re.sub(r"<[^>]+>", " ", text).strip().lower() or "(pdf)")
            for href, text in re.findall(
                r"<a[^>]+href=[\"']([^\"']+\.pdf)[\"'][^>]*>(.*?)</a>", page_html, re.S | re.I
            )
        ]
        discovered = _terms_links(page_html, url) + _terms_url_strings(page_html, url) + pdf_links
        deeper = []
        for u, l in discovered:
            if _registrable_domain(u) != page_domain:
                continue
            if u not in seen and u not in [d[0] for d in deeper]:
                deeper.append((u, l))
        for sub_index, (sub_url, sub_label) in enumerate(deeper[:8], 1):
            seen.add(sub_url)
            net.polite_sleep()
            _save_terms(session, sub_url, sub_label, save_dir / f"{host}.terms-{index}.{sub_index}.txt")


ROBOTS_SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "docs" / "robots"


def _nominatim_allowed(session: requests.Session) -> bool:
    """Ask Nominatim's own robots.txt before using it.

    This project spends a whole document (docs/podminky.md) on whether it is
    entitled to fetch what it fetches. Helping itself to someone else's
    geocoder without reading their rules first would make that document
    worthless.
    """
    try:
        resp = session.get("https://nominatim.openstreetmap.org/robots.txt", timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"  could not read Nominatim's robots.txt ({exc}) - treating as disallowed")
        return False
    if resp.status_code != 200:
        print(f"  Nominatim robots.txt returned HTTP {resp.status_code} - treating as disallowed")
        return False
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(resp.text.splitlines())
    allowed = rp.can_fetch(net.USER_AGENT, NOMINATIM_URL)
    print(f"  Nominatim robots.txt: /search is {'ALLOWED' if allowed else 'DISALLOWED'} for this User-Agent")
    if not allowed:
        print("  --- Nominatim robots.txt verbatim ---")
        for line in resp.text.splitlines():
            print(f"    {line}")
    return allowed


def _wikipedia_search_title(session: requests.Session, term: str) -> "str | None":
    """Best-matching Czech Wikipedia article title for a search term.

    Needed because exact titles guessed from memory miss: "Hostivarsky
    lesopark" and "Roztyly" both came back empty on the first run, and a
    coordinate that silently fails to resolve is how the collection area ends
    up quietly missing one of the places it was asked to watch.
    """
    try:
        resp = session.get(
            "https://cs.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": term,
                    "srlimit": 1, "format": "json", "formatversion": 2},
            timeout=TIMEOUT,
        )
        hits = resp.json().get("query", {}).get("search", [])
    except (requests.RequestException, ValueError, AttributeError):
        return None
    return hits[0]["title"] if hits else None


def _wikipedia_coordinates(session: requests.Session, title: str) -> "dict | None":
    """Coordinates from the Czech Wikipedia's public API.

    Wikipedia publishes this API precisely so that programs can use it, which
    makes it the fallback that needs no argument. Titles are Czech article
    names, so this resolves the places the way a Czech reader would name them.
    """
    try:
        resp = session.get(
            "https://cs.wikipedia.org/w/api.php",
            params={"action": "query", "prop": "coordinates", "titles": title,
                    "format": "json", "formatversion": 2},
            timeout=TIMEOUT,
        )
        pages = resp.json().get("query", {}).get("pages", [])
    except (requests.RequestException, ValueError, AttributeError) as exc:
        print(f"  {title}: wikipedia lookup failed ({exc})")
        return None
    for page in pages:
        coords = page.get("coordinates") or []
        if coords:
            return {"lat": float(coords[0]["lat"]), "lon": float(coords[0]["lon"]),
                    "display_name": page.get("title"), "via": "cs.wikipedia.org API"}
    return None


def resolve_reference_places(session: requests.Session, save_dir: "Path | None") -> None:
    """Look up the collection area's reference points and save them.

    The collection area is built from these coordinates, so getting them from
    memory is not good enough - an earlier hand-written value put "sporilov"
    about 1.5 km from Sporilov, which would have meant watching the wrong
    square kilometre for months with nothing looking wrong. Resolved here and
    written to the repo with the query sent, the name that came back and which
    service answered, so every number in the area definition can be traced.

    Two services, in this order: Nominatim if its own robots.txt permits it,
    and the Czech Wikipedia API, which exists to be called, for anything
    Nominatim did not answer.
    """
    hr("reference coordinates")
    resolved: dict[str, dict] = {}

    if _nominatim_allowed(session):
        for place in REFERENCE_PLACES:
            net.polite_sleep(1.0, 1.5)  # Nominatim asks for at most one request a second
            try:
                resp = session.get(
                    NOMINATIM_URL,
                    params={"q": place, "format": "jsonv2", "limit": 1, "accept-language": "cs"},
                    timeout=TIMEOUT,
                )
                hits = resp.json() if resp.status_code == 200 else []
            except (requests.RequestException, ValueError) as exc:
                print(f"  {place}: lookup failed ({exc})")
                continue
            if not hits:
                print(f"  {place}: no match")
                continue
            hit = hits[0]
            resolved[place] = {
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "display_name": hit.get("display_name"),
                "osm_type": hit.get("osm_type"),
                "type": hit.get("type"),
                "via": "nominatim.openstreetmap.org",
            }
            print(f"  {place:44s} -> {float(hit['lat']):.5f}, {float(hit['lon']):.5f}"
                  f"   {hit.get('display_name', '')[:55]}")
    else:
        print("  skipping Nominatim entirely; falling back to Wikipedia")

    for place, title in WIKIPEDIA_FALLBACK.items():
        if place in resolved:
            continue
        net.polite_sleep(0.5, 1.0)
        found = _wikipedia_coordinates(session, title)
        if found is None:
            # The guessed title was wrong, not the place missing: ask
            # Wikipedia's own search which article this actually is.
            net.polite_sleep(0.5, 1.0)
            searched = _wikipedia_search_title(session, title)
            if searched and searched != title:
                net.polite_sleep(0.5, 1.0)
                found = _wikipedia_coordinates(session, searched)
                if found is not None:
                    found["via"] += " (title found by search)"
                    title = searched
        if found is None:
            print(f"  {place:44s} -> not found on cs.wikipedia either")
            continue
        resolved[place] = found
        print(f"  {place:44s} -> {found['lat']:.5f}, {found['lon']:.5f}   (wikipedia: {title})")

    if save_dir is None or not resolved:
        print("  nothing resolved - collection area constants left untouched")
        return
    target = save_dir.parent / "geo" / "reference_points.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "_fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "_note": (
                    "Resolved by tools/probe_sources.py. Keys are the exact queries sent; "
                    "display_name is what the service matched them to, so a wrong match is "
                    "visible rather than silent, and 'via' says which service answered. "
                    "common/collection_area.py carries these numbers as constants."
                ),
                "places": resolved,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  saved -> {target}  ({len(resolved)} places)")


def dump_markup(session: requests.Session, name: str, url: str, save_dir: "Path | None",
                anchors: list, window: int = 1400) -> None:
    """Save the raw markup around the things a parser has to find.

    The iDNES detail page carries no schema.org block and no obvious
    coordinates, so there is nothing standard to parse and the only way to
    write a correct parser is to look at the actual HTML. Rather than dump
    170 KB, this saves a window around each anchor string - the price, the
    area, a listing link - which is where the useful attributes live.
    """
    hr(f"markup — {name}")
    try:
        resp = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"  could not fetch: {exc}")
        return
    if resp.status_code != 200:
        print(f"  HTTP {resp.status_code}")
        return
    html = resp.text
    out = [f"# Markup windows from {url}",
           f"# Fetched {datetime.now(timezone.utc).isoformat(timespec='seconds')}, {len(html)} bytes", ""]
    for anchor in anchors:
        matches = list(re.finditer(anchor, html, re.I))
        out.append(f"## anchor {anchor!r}: {len(matches)} match(es)")
        for match in matches[:2]:
            start = max(0, match.start() - window // 3)
            out.append(html[start:match.start() + window])
            out.append("  ---")
        out.append("")
    if save_dir is not None:
        target = save_dir.parent / "sources" / f"{name}.markup.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(out), encoding="utf-8")
        print(f"  saved -> {target}")


def main() -> int:
    """Refresh everything this project's compliance and parsing rest on.

    Not a data collector: it fetches the rules, the contracts, the reference
    coordinates the collection area is built from, and one page of real
    markup for the parser tests to assert against. Everything it writes goes
    into docs/ and is committed, so a change at a portal arrives as a diff.
    """
    session = net.build_session()
    print(f"User-Agent: {net.USER_AGENT}")
    save_dir = ROBOTS_SNAPSHOT_DIR

    for origin, paths in PORTALS:
        probe_robots(session, origin, paths, save_dir)

    # Only the portals this project actually reads, or argues about reading.
    for origin in ("https://www.sreality.cz", "https://www.bezrealitky.cz",
                   "https://reality.idnes.cz", "https://www.realingo.cz"):
        find_terms_links(session, origin, save_dir)

    # The markup tests/test_idnes.py parses. Saved rather than invented,
    # because a parser tested against invented HTML only proves that the
    # invention matches the parser.
    dump_markup(
        session, "reality.idnes.cz.card",
        "https://reality.idnes.cz/s/prodej/byty/praha/", save_dir,
        [r'href="[^"]*/detail/prodej/byt/[^"]*"'],
    )

    resolve_reference_places(session, save_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
