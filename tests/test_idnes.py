"""Reality.iDNES.cz.

The fixture markup in these tests is real: it was taken from a live search
page and a live detail page by tools/probe_sources.py and is stored in
docs/sources/. Testing an HTML parser against invented HTML proves only that
the invention matches the parser.
"""

import pytest

from common.budget import Budget
from scrapers import idnes
from scrapers.idnes import (
    fetch_all,
    page_url,
    parse_card,
    parse_price,
    parse_search_page,
    parse_title,
)

CARD = '''<div class="c-products__item">
<a href="https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a9981eb476061d4150b33d8/"
   class="c-products__link">
<div class="c-products__content">
<h2 class="c-products__title"><span class="text-capitalize">prodej</span> bytu 2+kk 55 m&sup2;</h2>
<p class="c-products__info">Nurmiho, Praha 15 - Hostiva&#345;</p>
<div class="c-products__footer"><p class="c-products__price"><strong>6 500 000 K&#269;</strong></p></div>
</div></a></div>'''


# --- reading one card -----------------------------------------------------


def test_a_card_yields_everything_but_the_coordinates():
    listing = parse_card(CARD)
    assert listing is not None
    assert listing.source == "idnes"
    assert listing.source_id == "6a9981eb476061d4150b33d8"
    assert listing.property_type == "byt"
    assert listing.transaction_type == "prodej"
    assert listing.disposition == "2+kk"
    assert listing.area_m2 == 55.0
    assert listing.price == 6_500_000
    assert listing.lat is None and listing.lon is None, "iDNES publishes no coordinates"
    assert listing.extra["placed_by"] == "address"


def test_a_watched_area_address_is_recognised_through_its_diacritics():
    """iDNES writes 'Hostivar' with the hacek; sreality gives an ASCII slug.
    Comparing them without folding matches one source and silently not the
    other."""
    assert parse_card(CARD).priority_zone is True


def test_an_address_elsewhere_in_prague_is_not_the_watched_area():
    card = CARD.replace("Nurmiho, Praha 15 - Hostiva&#345;", "Plze&#328;sk&aacute;, Praha 5 - Sm&iacute;chov")
    assert parse_card(card).priority_zone is False


def test_houses_are_skipped_because_the_brief_asks_for_flats():
    assert parse_card(CARD.replace("/byt/", "/dum/")) is None


def test_a_block_without_a_listing_link_is_not_a_listing():
    assert parse_card('<div class="c-products__item">advertisement</div>') is None


@pytest.mark.parametrize("raw,expected", [
    ("7 752 704 K&#269;", 7_752_704),
    ("19 800 000 Kč", 19_800_000),
    ("<strong>15 000 Kč/měsíc</strong>", 15_000),
])
def test_prices_survive_the_spaces_and_the_currency(raw, expected):
    assert parse_price(raw) == expected


def test_a_hidden_price_is_none_rather_than_zero():
    """A zero here would poison every average this dataset exists to compute."""
    assert parse_price("Info o ceně u RK") is None
    assert parse_price("") is None


@pytest.mark.parametrize("title,disposition,area", [
    ("prodej bytu 1+kk 38 m²", "1+kk", 38.0),
    ("pronájem bytu 2+1 55 m²", "2+1", 55.0),
    ("prodej bytu 3+kk 102 m²", "3+kk", 102.0),
])
def test_disposition_and_area_come_out_of_the_title(title, disposition, area):
    assert parse_title(title) == {"disposition": disposition, "area_m2": area}


def test_an_unreadable_title_loses_the_fields_rather_than_the_listing():
    assert parse_title("prodej nemovitosti") == {"disposition": None, "area_m2": None}


# --- a whole page ---------------------------------------------------------


def test_one_malformed_card_does_not_cost_the_page():
    page = CARD + '<div class="c-products__item">broken' + CARD.replace(
        "6a9981eb476061d4150b33d8", "6aa9655ec4dbddff1f057953")
    assert len(parse_search_page(page)) == 2


def test_the_real_saved_markup_still_parses():
    """Guards against iDNES changing its class names: this is the actual HTML
    the parser was written against."""
    from pathlib import Path

    saved = Path(__file__).resolve().parent.parent / "docs" / "sources" / "reality.idnes.cz.card.markup.txt"
    if not saved.exists():
        pytest.skip("markup snapshot not present")
    listings = parse_search_page(saved.read_text(encoding="utf-8"))
    assert listings, "the saved real markup must still yield listings"
    assert listings[0].price and listings[0].address


def test_page_url_leaves_the_first_page_unparameterised():
    assert page_url("byt", "prodej", 1) == "https://reality.idnes.cz/s/prodej/byty/praha/"
    assert page_url("byt", "prodej", 7) == "https://reality.idnes.cz/s/prodej/byty/praha/?page=7"


# --- the run: priorities and resumption -----------------------------------


class FakeIdnes:
    """Serves `pages_per_scope` pages of cards, then empty pages."""

    def __init__(self, pages_per_scope=5):
        self.pages_per_scope = pages_per_scope
        self.requested: list = []

    def fetch_text(self, session, url, **kwargs):
        self.requested.append(url)
        if "/detail/" in url:
            return '<meta property="og:description" content="Popis bytu.">'
        page = 1
        if "page=" in url:
            page = int(url.split("page=")[1])
        if page > self.pages_per_scope:
            return "<html>nic nenalezeno</html>"
        transaction = "prodej" if "/prodej/" in url else "pronajem"
        return "".join(
            CARD.replace("6a9981eb476061d4150b33d8", f"{page:02d}{n:02d}" + "0" * 20)
                .replace("/prodej/", f"/{transaction}/")
            for n in range(3)
        )


@pytest.fixture
def fake_idnes(monkeypatch):
    fake = FakeIdnes()
    monkeypatch.setattr(idnes.net, "fetch_text", fake.fetch_text)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)
    return fake


def test_a_run_reads_the_newest_pages_first(fake_idnes):
    """A listing that appeared in the last hour is the thing most worth
    knowing about, so page 1 is never at the mercy of the rotation."""
    fetch_all(None, Budget(max_seconds=60, max_new_details=None), known={}, page_cursor={}, max_pages=2)
    first = fake_idnes.requested[0]
    assert first.endswith("/s/prodej/byty/praha/"), "the newest page must be read before anything else"


def test_the_index_rotation_continues_where_the_last_run_stopped(fake_idnes):
    _, _, _, _, _, cursors = fetch_all(
        None, Budget(max_seconds=60, max_new_details=None), known={}, page_cursor={}, max_pages=4
    )
    assert cursors["byt/prodej"] > 1, "the cursor must move so the next run starts further on"

    fake_idnes.requested.clear()
    fetch_all(None, Budget(max_seconds=60, max_new_details=None), known={}, page_cursor=cursors, max_pages=4)
    rotation_pages = [u for u in fake_idnes.requested if "page=" in u and "/prodej/" in u]
    assert any(f"page={cursors['byt/prodej']}" in u for u in rotation_pages), \
        "the second run must resume at the stored page"


def test_reaching_the_end_of_the_pages_marks_the_scope_complete_and_wraps(fake_idnes):
    _, _, _, completed, _, cursors = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None), known={},
        page_cursor={"byt/prodej": 5, "byt/pronajem": 5}, max_pages=8,
    )
    assert ("byt", "prodej") in completed
    assert cursors["byt/prodej"] == 1, "a finished pass restarts rather than running off the end"


def test_known_watched_area_listings_are_re_read_every_run(fake_idnes):
    known = {
        "aaa": {"source_id": "aaa", "url": "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/a/",
                "address": "Nurmiho, Praha 15 - Hostivař"},
        "bbb": {"source_id": "bbb", "url": "https://reality.idnes.cz/detail/prodej/byt/praha-5-plzenska/b/",
                "address": "Plzeňská, Praha 5 - Smíchov"},
    }
    fetch_all(None, Budget(max_seconds=600, max_new_details=None), known=known, page_cursor={}, max_pages=2)
    details = [u for u in fake_idnes.requested if "/detail/" in u]
    assert any("nurmiho" in u for u in details), "the watched area must be refreshed every run"
    assert not any("plzenska" in u for u in details), "the rest of Prague waits for its turn"


def test_an_exhausted_budget_stops_the_run_without_losing_what_it_had(fake_idnes):
    spent = Budget(max_seconds=0, max_new_details=None)
    listings, _, errors, completed, _, _ = fetch_all(None, spent, known={}, page_cursor={}, max_pages=8)
    assert completed == set(), "absence must never be inferred from a run that was cut short"
    assert any("budget" in e for e in errors)


# --- the detail page, which is one meta tag -------------------------------

from scrapers.idnes import parse_og_description

DETAIL_URL = "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a3a8eea20379d6ad200f2ed/"


def test_the_whole_listing_comes_out_of_og_description():
    """iDNES fills this tag for its own link previews, so it carries every
    field and survives the markup being redesigned."""
    listing = parse_og_description(
        "Prodej bytu 3+1 80 m², Nurmiho, Praha 15 - Hostivař. Cena 8 900 000 Kč. "
        "Pěkný byt u lesoparku.",
        DETAIL_URL,
    )
    assert listing.disposition == "3+1"
    assert listing.area_m2 == 80.0
    assert listing.price == 8_900_000
    assert listing.address == "Nurmiho, Praha 15 - Hostivař"
    assert listing.description == "Pěkný byt u lesoparku."
    assert listing.priority_zone is True


def test_agency_boilerplate_is_not_kept_as_the_description():
    """Stopping at the first full stop does not work: agency names are full
    of them."""
    listing = parse_og_description(
        "Prodej bytu 2+kk 65 m², Ulice, Praha 4. Cena 1 Kč. "
        "Nabízí realitní kancelář PSN s.r.o.. Plně dokončený loft s terasou.",
        DETAIL_URL,
    )
    assert listing.description == "Plně dokončený loft s terasou."


def test_a_description_with_no_agency_sentence_is_kept_whole():
    listing = parse_og_description(
        "Prodej bytu 2+kk 65 m², Ulice, Praha 4. Cena 1 Kč. Byt bez makléře.", DETAIL_URL
    )
    assert listing.description == "Byt bez makléře."


def test_a_listing_with_no_stated_price_still_parses():
    listing = parse_og_description(
        "Prodej bytu 2+kk 50 m², Ulice, Praha 4. Popis bytu.", DETAIL_URL
    )
    assert listing is not None and listing.price is None


def test_rent_is_recognised_as_rent():
    listing = parse_og_description(
        "Pronájem bytu 3+kk 123 m², Truhlářská, Praha 1. Cena 45 000 Kč. Hezký byt.",
        DETAIL_URL.replace("/prodej/", "/pronajem/"),
    )
    assert listing.transaction_type == "pronajem"


def test_an_unparseable_tag_returns_nothing_rather_than_a_wrong_listing():
    assert parse_og_description("Reality.iDNES.cz - nejlepší nabídky", DETAIL_URL) is None
    assert parse_og_description("Prodej bytu 2+kk 65 m², Ulice, Praha 4.", "https://example.com/x") is None


def test_the_watched_area_refresh_replaces_the_thinner_index_row(fake_idnes, monkeypatch):
    """The point of spending a request on a detail page: the row that lands
    must be the one carrying the description, not the index card."""
    monkeypatch.setattr(
        idnes, "fetch_detail",
        lambda session, url: (
            parse_og_description(
                "Prodej bytu 2+kk 55 m², Nurmiho, Praha 15 - Hostivař. Cena 6 500 000 Kč. Popis.",
                "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a9981eb476061d4150b33d8/",
            ),
            None,
        ),
    )
    known = {"x": {"source_id": "6a9981eb476061d4150b33d8",
                   "url": "https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/6a9981eb476061d4150b33d8/",
                   "address": "Nurmiho, Praha 15 - Hostivař"}}
    listings, _, _, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None), known=known, page_cursor={}, max_pages=2
    )
    matching = [l for l in listings if l.source_id == "6a9981eb476061d4150b33d8"]
    assert len(matching) == 1, "the listing must not appear twice"
    assert matching[0].description == "Popis.", "the detail version must win over the index card"


def test_the_watched_area_refresh_is_capped_and_takes_the_oldest_first(fake_idnes):
    """Unbounded, this loop spends one request per watched listing before the
    rotation gets a single page, so a busy Hostivar would quietly consume the
    hour the rest of Prague is meant to share."""
    known = {
        f"id{n}": {
            "source_id": f"id{n}",
            "url": f"https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/{n}/",
            "address": "Nurmiho, Praha 15 - Hostivař",
            # id0 is the stalest, id19 the freshest.
            "last_seen_at": f"2026-09-{(n % 20) + 1:02d}",
        }
        for n in range(40)
    }
    fetch_all(None, Budget(max_seconds=600, max_new_details=None), known=known,
              page_cursor={}, max_pages=2, max_watched=5)
    details = [u for u in fake_idnes.requested if "/detail/" in u]
    assert len(details) == 5, "the cap must hold"
    # The five oldest have last_seen_at 2026-09-01, i.e. ids 0 and 20.
    assert any(u.endswith("/0/") for u in details), "the stalest must be refreshed first"


def test_a_small_watched_area_is_refreshed_entirely(fake_idnes):
    known = {
        f"id{n}": {
            "source_id": f"id{n}",
            "url": f"https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/{n}/",
            "address": "Nurmiho, Praha 15 - Hostivař",
            "last_seen_at": "2026-09-15",
        }
        for n in range(4)
    }
    fetch_all(None, Budget(max_seconds=600, max_new_details=None), known=known,
              page_cursor={}, max_pages=2)
    assert len([u for u in fake_idnes.requested if "/detail/" in u]) == 4


# --- a detail page that renders from another template ---------------------


class UnparseableDetails(FakeIdnes):
    """Serves detail pages that load fine but carry no og:description.

    These are real: iDNES renders developer-project listings from a different
    template, and there are some in the watched area right now on Honzikova
    and U zakrutu. Each one used to be reported as an error, which turned
    every run red from the moment the second was discovered - and the count
    grew hour by hour as more watched-area listings accumulated.
    """

    def fetch_text(self, session, url, **kwargs):
        if "/detail/" in url:
            self.requested.append(url)
            return "<html><head><title>Projekt</title></head><body>…</body></html>"
        return super().fetch_text(session, url, **kwargs)


def watched(count):
    return {
        f"id{n}": {
            "source_id": f"id{n}",
            "url": f"https://reality.idnes.cz/detail/prodej/byt/praha-15-nurmiho/{n}/",
            "address": "Nurmiho, Praha 15 - Hostivař",
            "last_seen_at": "2026-09-15",
        }
        for n in range(count)
    }


def test_a_page_without_og_description_is_not_an_error(monkeypatch):
    fake = UnparseableDetails()
    monkeypatch.setattr(idnes.net, "fetch_text", fake.fetch_text)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)

    _, _, errors, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None),
        known=watched(3), page_cursor={}, max_pages=2,
    )
    real = [e for e in errors if "budget" not in e]
    assert real == [], f"a differently-rendered listing must not fail the run: {real}"


def test_the_card_already_supplied_what_the_detail_was_for(monkeypatch):
    """Nothing is lost when a detail page does not parse: price, area,
    disposition and address all came from the search card."""
    fake = UnparseableDetails()
    monkeypatch.setattr(idnes.net, "fetch_text", fake.fetch_text)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)

    listings, _, _, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None),
        known=watched(2), page_cursor={}, max_pages=2,
    )
    assert listings, "the index listings must still come through"
    assert all(l.price and l.area_m2 for l in listings)


def test_wholesale_unparseable_details_do_raise_the_alarm(monkeypatch):
    """The case that really is a failure: iDNES changing its markup, so that
    no detail page parses any more."""
    fake = UnparseableDetails()
    monkeypatch.setattr(idnes.net, "fetch_text", fake.fetch_text)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)

    _, _, errors, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None),
        known=watched(30), page_cursor={}, max_pages=2,
    )
    assert any("markup has most likely changed" in e for e in errors), errors


def test_a_handful_of_odd_listings_does_not_raise_the_alarm(monkeypatch):
    """The threshold needs a floor as well as a ratio, or two odd listings
    out of three would be enough to cry wolf."""
    fake = UnparseableDetails()
    monkeypatch.setattr(idnes.net, "fetch_text", fake.fetch_text)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)

    _, _, errors, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None),
        known=watched(idnes.UNPARSED_ALARM_MIN - 1), page_cursor={}, max_pages=2,
    )
    assert not any("markup" in e for e in errors), errors


def test_a_network_failure_on_a_detail_page_is_still_an_error(monkeypatch):
    """Softening the parse failure must not soften the fetch failure."""
    def explode(session, url, **kwargs):
        if "/detail/" in url:
            raise idnes.net.RequestFailed("HTTP 500")
        return "<html>nic</html>"

    monkeypatch.setattr(idnes.net, "fetch_text", explode)
    monkeypatch.setattr(idnes.net, "polite_sleep", lambda *a, **k: None)
    _, _, errors, _, _, _ = fetch_all(
        None, Budget(max_seconds=600, max_new_details=None),
        known=watched(2), page_cursor={}, max_pages=2,
    )
    assert any("HTTP 500" in e for e in errors), errors


# --- the end of the index ---------------------------------------------------


def _walk(monkeypatch, responder):
    """Run the index walk against a fake iDNES."""
    from common import net
    from scrapers import idnes as mod

    monkeypatch.setattr(net, "polite_sleep", lambda *a, **k: None)
    monkeypatch.setattr(net, "fetch_text", responder)
    return mod.fetch_all(object(), None, known={}, page_cursor={},
                         max_pages=50, transactions=["pronajem"])


def test_a_404_past_the_first_page_ends_the_index_rather_than_failing(monkeypatch):
    """iDNES answers 404 past the last page instead of serving an empty one.

    Treating that as a broken request cost more than a spurious error line:
    the walk only marks a scope complete when it reaches the end, and a scope
    that is never complete is never absence-marked - so no iDNES listing could
    ever be found to have gone. Seen live at page 114 of the rentals.
    """
    from common import net

    def responder(session, url, **kwargs):
        if "page=" in url and int(url.rsplit("page=", 1)[1]) >= 3:
            raise net.RequestFailed(f"HTTP 404 for {url}", status=404)
        return "<html></html>"

    _, _, errors, completed, _, cursors = _walk(monkeypatch, responder)

    assert errors == [], f"the end of the index was reported as a failure: {errors}"
    assert ("byt", "pronajem") in completed, \
        "the scope must be marked complete, or absence is never judged"
    assert cursors["byt/pronajem"] == 1, "the next pass starts from the front"


def test_a_404_on_the_first_page_is_still_a_failure(monkeypatch):
    """That is the search URL itself having changed, which must stay loud."""
    from common import net

    def responder(session, url, **kwargs):
        raise net.RequestFailed(f"HTTP 404 for {url}", status=404)

    _, _, errors, completed, _, _ = _walk(monkeypatch, responder)
    assert errors, "a 404 on page one was swallowed"
    assert ("byt", "pronajem") not in completed


def test_other_http_errors_are_still_failures(monkeypatch):
    from common import net

    def responder(session, url, **kwargs):
        if "page=" in url:
            raise net.RequestFailed(f"HTTP 403 for {url}", status=403)
        return "<html></html>"

    _, _, errors, _, _, _ = _walk(monkeypatch, responder)
    assert errors, "a 403 must not be mistaken for the end of the index"
