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
]

DETAIL_LOCALITY_RE = re.compile(
    r"reality\.idnes\.cz/detail/(?:prodej|pronajem)/(?:byt|dum)/([^/]+)/")


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
    for slug, count in slugs.most_common(8):
        print(f"       {slug:<44} {count}")
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
    control_slugs = control.get("slugs") or Counter()
    for result in results[1:]:
        label = result["label"]
        if not result.get("slugs"):
            print(f"  {label:<38} nepouzitelne "
                  f"({result.get('status') or result.get('error') or 'robots'})")
            continue
        slugs = result["slugs"]
        # A locality filter that works returns listings from that locality.
        # One that is ignored returns the same spread the control does.
        shared = sum((slugs & control_slugs).values())
        overlap = shared / max(sum(slugs.values()), 1)
        verdict = ("FILTRUJE" if len(slugs) <= 3 and overlap < 0.9
                   else "ignoruje filtr (vraci celou Prahu)")
        print(f"  {label:<38} HTTP {result['status']}  "
              f"lokalit={len(slugs):<4} {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
