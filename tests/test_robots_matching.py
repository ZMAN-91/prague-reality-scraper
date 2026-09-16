"""robots.txt matching, including the wildcards the standard library drops.

Python's urllib.robotparser compares paths with startswith, so "Disallow:
/vyhledat*" matches nothing at all - nothing real begins with that literal
string. That is how bezrealitky writes all four of its prohibitions and how
Reality.iDNES.cz writes nearly all of its, so the compliance check this
project leans on was silently ignoring most of the rules it was checking.
"""

from pathlib import Path

import pytest

from common import net
from common.robots import can_fetch, parse

UA = "PragueRealitySectorAnalysis/1.0 (+https://example.com; contact)"
ROBOTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "robots"


def snapshot(host: str) -> str:
    path = ROBOTS_DIR / f"{host}.robots.txt"
    if not path.exists():
        pytest.skip(f"no snapshot for {host}")
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("# ")
    )


# --- the bug this module exists for ---------------------------------------


BEZREALITKY = """User-agent: *
Disallow: /vyhledat*
Disallow: /moje-bezrealitky/*
Disallow: /*callbackUrl=*
Disallow: /search*
Sitemap: https://www.bezrealitky.cz/sitemap/sitemap.xml
"""


@pytest.mark.parametrize("url", [
    "https://www.bezrealitky.cz/vyhledat",
    "https://www.bezrealitky.cz/vyhledat?offerType=PRODEJ",
    "https://www.bezrealitky.cz/vyhledat/praha",
    "https://www.bezrealitky.cz/search?q=byt",
    "https://www.bezrealitky.cz/moje-bezrealitky/oblibene",
    "https://www.bezrealitky.cz/cokoliv?callbackUrl=/x",
])
def test_a_wildcard_rule_actually_forbids_something(url):
    assert can_fetch(BEZREALITKY, UA, url) is False


def test_what_the_wildcards_do_not_cover_stays_allowed():
    assert can_fetch(
        BEZREALITKY, UA,
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho",
    ) is True
    assert can_fetch(BEZREALITKY, UA, "https://www.bezrealitky.cz/sitemap/sitemap.xml") is True


# --- RFC 9309 precedence --------------------------------------------------


def test_the_most_specific_rule_wins_not_the_first():
    rules = """User-agent: *
Disallow: /hledani/
Allow: /hledani/praha/
"""
    assert can_fetch(rules, UA, "https://x.cz/hledani/brno/") is False
    assert can_fetch(rules, UA, "https://x.cz/hledani/praha/") is True


def test_allow_wins_a_tie_with_disallow():
    rules = "User-agent: *\nDisallow: /a\nAllow: /a\n"
    assert can_fetch(rules, UA, "https://x.cz/a") is True


def test_a_dollar_anchors_the_end_of_the_path():
    rules = "User-agent: *\nDisallow: /page$\n"
    assert can_fetch(rules, UA, "https://x.cz/page") is False
    assert can_fetch(rules, UA, "https://x.cz/page/more") is True


def test_an_empty_disallow_forbids_nothing():
    """"Disallow:" with nothing after it is how a site says "help yourself" -
    sreality uses exactly this for the search engines it welcomes."""
    assert can_fetch("User-agent: *\nDisallow:\n", UA, "https://x.cz/anything") is True


def test_silence_is_permission():
    assert can_fetch("User-agent: *\nAllow: /\n", UA, "https://x.cz/whatever") is True
    assert can_fetch("# just a comment\n", UA, "https://x.cz/whatever") is True


def test_a_group_naming_us_replaces_the_wildcard_group():
    rules = """User-agent: *
Disallow: /

User-agent: PragueRealitySectorAnalysis
Allow: /
"""
    assert can_fetch(rules, UA, "https://x.cz/anything") is True


def test_comments_and_odd_spacing_do_not_break_a_rule():
    rules = "User-agent: *  # everyone\n  Disallow:  /private*   # keep out\n"
    assert can_fetch(rules, UA, "https://x.cz/private/x") is False


def test_a_pattern_full_of_regex_metacharacters_is_taken_literally():
    """"/hledani/*%2C" is a real rule on sreality; a pattern compiled
    carelessly either crashes or matches the wrong thing."""
    rules = "User-agent: *\nDisallow: /hledani/*%2C\nDisallow: /a+b(c)\n"
    assert can_fetch(rules, UA, "https://x.cz/hledani/praha%2Cbrno") is False
    assert can_fetch(rules, UA, "https://x.cz/a+b(c)") is False
    assert can_fetch(rules, UA, "https://x.cz/aaab") is True


# --- against the real snapshots -------------------------------------------


def test_sreality_still_disallows_everything_for_us():
    assert can_fetch(snapshot("www.sreality.cz"), net.USER_AGENT,
                     "https://www.sreality.cz/api/v1/estates/search?limit=1") is False


def test_a_search_engine_is_still_welcome_on_sreality():
    """Sanity check on the parser itself: the eighteen named agents must come
    out allowed, or the group handling is wrong rather than the site."""
    assert can_fetch(snapshot("www.sreality.cz"), "Googlebot/2.1",
                     "https://www.sreality.cz/hledani/prodej/byty/praha") is True


def test_the_bezrealitky_api_host_is_closed():
    assert can_fetch(snapshot("api.bezrealitky.cz"), net.USER_AGENT,
                     "https://api.bezrealitky.cz/graphql/") is False


def test_the_stricter_matcher_still_permits_everything_we_fetch():
    """The point of tightening the check is to be honest, not to lock the
    project out of the routes it was told it may use."""
    from scrapers.idnes import SEARCH_URLS, page_url

    idnes = snapshot("reality.idnes.cz")
    for _, transaction in SEARCH_URLS:
        for page in (1, 2, 7, 120, 257):
            url = page_url("byt", transaction, page)
            assert can_fetch(idnes, net.USER_AGENT, url) is True, url
    assert can_fetch(
        idnes, net.USER_AGENT,
        "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a3a8eea20379d6ad200f2ed/",
    ) is True

    bez = snapshot("www.bezrealitky.cz")
    for url in (
        "https://www.bezrealitky.cz/sitemap/sitemap.xml",
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha",
    ):
        assert can_fetch(bez, net.USER_AGENT, url) is True, url
