"""Every URL a scraper builds, checked against that portal's own robots.txt.

This is the pre-flight check for a failure that is otherwise invisible until
it has already happened: a URL shape that robots.txt forbids gets refused by
common/net.py, the scraper returns nothing, and the source silently
contributes no data. The empty-result alarm catches that, but only after a
run has been wasted - and only if something was already stored.

The rules are read from the snapshots in docs/robots/, which
tools/probe_sources.py fetched verbatim from each portal. That means this
test also fails when a portal *changes* its rules under us, which is the
other half of what it is for.
"""

from pathlib import Path

import pytest

from common import net
from common import robots as robots_rules

ROBOTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "robots"


class _Rules:
    """The snapshot's rules, matched the way common/robots.py matches them -
    which is the way the portals wrote them. urllib.robotparser cannot be
    used here: it drops every wildcard rule, which is most of them."""

    def __init__(self, text: str):
        self.text = text

    def can_fetch(self, agent: str, url: str) -> bool:
        return robots_rules.can_fetch(self.text, agent, url)


def rules_for(host: str) -> "_Rules":
    snapshot = ROBOTS_DIR / f"{host}.robots.txt"
    if not snapshot.exists():
        pytest.skip(f"no robots.txt snapshot for {host}")
    # Drop the snapshot header the probe writes, keep the file verbatim.
    body = "\n".join(
        line for line in snapshot.read_text(encoding="utf-8").splitlines()
        if not line.startswith("# ")
    )
    return _Rules(body)


def test_every_idnes_url_the_scraper_builds_is_permitted():
    from scrapers.idnes import SEARCH_URLS, page_url

    rules = rules_for("reality.idnes.cz")
    urls = [page_url("byt", transaction, page)
            for _, transaction in SEARCH_URLS
            for page in (1, 2, 7, 120, 257)]
    urls.append(
        "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a3a8eea20379d6ad200f2ed/"
    )
    blocked = [url for url in urls if not rules.can_fetch(net.USER_AGENT, url)]
    assert blocked == [], f"robots.txt forbids URLs this scraper builds: {blocked}"


def test_bezrealitky_listing_pages_are_permitted_and_its_search_is_not():
    """The distinction the whole bezrealitky scraper is built around: its
    detail pages are open, its search is not, so the sitemap is the route."""
    rules = rules_for("www.bezrealitky.cz")
    assert rules.can_fetch(
        net.USER_AGENT,
        "https://www.bezrealitky.cz/nemovitosti-byty-domy/1067272-nabidka-prodej-bytu-nurmiho-praha",
    )
    assert rules.can_fetch(net.USER_AGENT, "https://www.bezrealitky.cz/sitemap/sitemap.xml")
    assert not rules.can_fetch(net.USER_AGENT, "https://www.bezrealitky.cz/vyhledat?offerType=PRODEJ")


def test_the_bezrealitky_graphql_host_is_still_off_limits():
    """Its API host is Disallow: / and the scraper must never drift back to
    it - the original version targeted it and was rewritten for this reason."""
    rules = rules_for("api.bezrealitky.cz")
    assert not rules.can_fetch(net.USER_AGENT, "https://api.bezrealitky.cz/graphql/")


def test_sreality_is_still_the_only_source_needing_an_override():
    """If this ever starts passing without the override, the override should
    be deleted - and if another source ever starts failing it, that is a
    decision to make deliberately, not to discover in a log."""
    rules = rules_for("www.sreality.cz")
    assert not rules.can_fetch(
        net.USER_AGENT,
        "https://www.sreality.cz/api/v1/estates/search?category_main_cb=1",
    ), "sreality now permits this; the override in scrape.yml can go"


def test_the_workflow_overrides_exactly_one_host():
    """The override is the single place this project stops treating a
    robots.txt as binding, and it should stay single."""
    workflow = (Path(__file__).resolve().parent.parent
                / ".github" / "workflows" / "scrape.yml").read_text(encoding="utf-8")
    line = [l for l in workflow.splitlines() if "SCRAPER_ROBOTS_OVERRIDE_HOSTS:" in l]
    assert len(line) == 1
    hosts = line[0].split(":", 1)[1].strip().strip('"').split(",")
    assert [h.strip() for h in hosts] == ["www.sreality.cz"]


def test_no_workflow_overrides_robots_for_any_other_host():
    """The override is per-workflow, so adding a workflow that touches a new
    host is exactly the moment one could quietly widen.

    It is not enough to check scrape.yml: probe-ruian.yml fetches from
    cuzk.cz, and an override copied into it - or into a workflow added later
    - would mean this project decided a public register's robots.txt did not
    apply to it, without anyone deciding that."""
    root = Path(__file__).resolve().parent.parent
    workflows = sorted((root / ".github" / "workflows").glob("*.yml"))
    workflows += sorted((root / "deploy").glob("*.yml"))
    assert workflows, "no workflows found; this test would pass vacuously"

    overriding = {}
    for path in workflows:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "SCRAPER_ROBOTS_OVERRIDE_HOSTS:" not in line:
                continue
            hosts = line.split(":", 1)[1].strip().strip('"')
            overriding.setdefault(path.name, []).extend(
                h.strip() for h in hosts.split(",") if h.strip())

    unexpected = {name: hosts for name, hosts in overriding.items()
                  if set(hosts) - {"www.sreality.cz"}}
    assert not unexpected, (
        f"a workflow overrides robots.txt for a host other than sreality: "
        f"{unexpected}"
    )
