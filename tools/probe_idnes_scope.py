"""Does iDNES let a search be narrowed to one part of Prague?

    python -m tools.probe_idnes_scope        (needs internet; run it on a runner)

The hourly pass walks `reality.idnes.cz/s/prodej/byty/praha/` - the whole
city, about 115 pages - and works out afterwards which listings fall in the
watched area. If the site accepts a locality in the search path, the watched
area could be asked for directly and the hourly pass would stop paging
through Prague to keep a twentieth of it.

Its own detail URLs are built as /detail/prodej/byt/praha-15-honzikova/<id>/,
so the site has locality slugs. Whether the SEARCH path takes them is the
question, and there is one trap: a URL that 200s and quietly ignores the
locality looks exactly like one that works. So this does not stop at the
status code - it reads which localities come back, and a filter that is
being ignored gives itself away by returning the whole city.

Read-only. A couple of dozen pages, nothing written, nothing committed.
"""

from __future__ import annotations

import re
import sys
from collections import Counter

from common import net
from scrapers.idnes import LISTING_HREF_RE

BASE = "https://reality.idnes.cz"

# Shapes worth trying, coarse to fine. The first is today's behaviour and is
# the control: whatever the others return has to be compared against it.
CANDIDATES = [
    ("cela Praha (dnesni stav)", f"{BASE}/s/prodej/byty/praha/"),
    ("obvod", f"{BASE}/s/prodej/byty/praha-10/"),
    ("obvod + katastr", f"{BASE}/s/prodej/byty/praha-10-hostivar/"),
    ("katastr", f"{BASE}/s/prodej/byty/hostivar/"),
    ("obvod + ulice (tvar z detail URL)", f"{BASE}/s/prodej/byty/praha-15-honzikova/"),
    ("dotaz v query", f"{BASE}/s/prodej/byty/praha/?s-qc%5BlocalityId%5D=hostivar"),
    # A second and third borough, because one page of one borough could look
    # filtered by chance, and a borough on the other side of the city cannot.
    ("obvod (Praha 6)", f"{BASE}/s/prodej/byty/praha-6/"),
    ("obvod (Praha 9)", f"{BASE}/s/prodej/byty/praha-9/"),
    ("obvod, druha strana", f"{BASE}/s/prodej/byty/praha-10/?page=2"),
    ("obvod, pronajmy", f"{BASE}/s/pronajem/byty/praha-10/"),
]

DETAIL_LOCALITY_RE = re.compile(
    r"reality\.idnes\.cz/detail/(?:prodej|pronajem)/(?:byt|dum)/([^/]+)/")


BOROUGH_RE = re.compile(r"^(praha-\d+)")


def borough_of(slug: str) -> str:
    """"praha-15-bolonska" -> "praha-15". The slug's coarse half."""
    match = BOROUGH_RE.match(slug or "")
    return match.group(1) if match else (slug or "?")


def localities(html: str) -> Counter:
    """Which locality slugs the listings on this page belong to."""
    return Counter(DETAIL_LOCALITY_RE.findall(html))


def probe(session, label: str, url: str) -> dict:
    print(f"\n--- {label}\n    {url}")
    if not net.is_allowed_by_robots(session, url):
        print("    robots.txt: NOT ALLOWED - skipped")
        return {"label": label, "url": url, "allowed": False}
    try:
        response = session.get(url, timeout=30, allow_redirects=True)
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        print(f"    request failed: {exc}")
        return {"label": label, "url": url, "error": str(exc)}

    html = response.text
    listings = len(set(LISTING_HREF_RE.findall(html)))
    slugs = localities(html)
    print(f"    HTTP {response.status_code}"
          + (f"  -> redirected to {response.url}" if response.url != url else ""))
    print(f"    inzeratu na strance: {listings}")
    print(f"    ruznych lokalit mezi nimi: {len(slugs)}")
    # Every one of them, not the top few: the question is what the SPREAD is,
    # and a truncated list cannot answer it. Counting distinct localities is
    # no answer either - a working borough filter still returns one locality
    # per street, so "24 localities" says nothing about whether the filter
    # bit. What says it is which boroughs they belong to.
    for slug, count in sorted(slugs.items()):
        print(f"       {slug:<44} {count}")
    boroughs = Counter(borough_of(slug) for slug in slugs.elements())
    print(f"    podle obvodu: "
          + ", ".join(f"{b}={n}" for b, n in boroughs.most_common()))
    net.polite_sleep()
    return {"label": label, "url": url, "status": response.status_code,
            "final_url": response.url, "listings": listings, "slugs": slugs}


def main() -> int:
    session = net.build_session()
    results = [probe(session, label, url) for label, url in CANDIDATES]

    print("\n" + "=" * 72)
    print("ZAVER")
    print("=" * 72)
    control = results[0]
    control_boroughs = Counter(
        borough_of(s) for s in (control.get("slugs") or Counter()).elements())
    print(f"  kontrola: {len(control_boroughs)} obvodu, "
          + ", ".join(f"{b}={n}" for b, n in control_boroughs.most_common()))
    for result in results[1:]:
        label = result["label"]
        if not result.get("slugs"):
            print(f"  {label:<38} nepouzitelne "
                  f"({result.get('status') or result.get('error') or 'robots'})")
            continue
        boroughs = Counter(borough_of(s) for s in result["slugs"].elements())
        total = max(sum(boroughs.values()), 1)
        # The verdict is the spread of BOROUGHS, against the control's. A
        # filter that bit returns a handful of adjacent ones; one that was
        # ignored returns as many as the unfiltered page did.
        verdict = ("FILTRUJE" if len(boroughs) <= max(3, len(control_boroughs) // 2)
                   else "ignoruje filtr")
        print(f"  {label:<38} HTTP {result['status']}  "
              f"obvodu={len(boroughs):<3} ({total} inzeratu)  {verdict}")
        print(f"      {', '.join(f'{b}={n}' for b, n in boroughs.most_common())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
