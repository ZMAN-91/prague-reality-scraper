"""Tests for the query/export tooling in tools/.

Until these existed the dataset was write-only: correct on disk, but with
no supported way to ask it a question. These tests cover the joins and the
matching rules, which is where a wrong answer would look plausible - an
empty street result is indistinguishable from "nothing is for sale there"
unless the matching itself is known to work.
"""

import csv
from pathlib import Path

from common.schema import LISTING_FIELDS
from tools.export_csv import export
from tools.filter_street import filter_street, slugify
from tools.listing_query import (
    as_bool,
    enrich,
    fold,
    is_live,
    load_latest_prices,
    match_street,
)


def write_dataset(data_dir: Path, listings: list[dict], observations: list[dict]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "listings.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LISTING_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in listings:
            writer.writerow(row)

    obs_dir = data_dir / "observations"
    obs_dir.mkdir(parents=True, exist_ok=True)
    by_month: dict[str, list[dict]] = {}
    for row in observations:
        by_month.setdefault(row["observed_at"][:7], []).append(row)
    for month, rows in by_month.items():
        with open(obs_dir / f"{month}.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f, fieldnames=["internal_id", "observed_at", "price", "price_per_m2", "status"]
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)


def listing(internal_id, **overrides):
    row = {field: "" for field in LISTING_FIELDS}
    row.update(
        {
            "internal_id": internal_id,
            "source": "sreality",
            "source_id": internal_id,
            "url": f"https://www.sreality.cz/detail/prodej/byt/2+kk/praha-sporilov-nurniho/{internal_id}",
            "property_type": "byt",
            "transaction_type": "prodej",
            "disposition": "2+kk",
            "area_m2": "55",
            "lat": "50.027",
            "lon": "14.478",
            "address": "Nurniho, Praha 4 - Sporilov",
            "priority_zone": "True",
            "first_seen_at": "2026-03-01T10:00:00+00:00",
            "last_seen_at": "2026-03-05",
            "status": "active",
        }
    )
    row.update(overrides)
    return row


def observation(internal_id, observed_at, price, status="active"):
    return {
        "internal_id": internal_id,
        "observed_at": observed_at,
        "price": price,
        "price_per_m2": "",
        "status": status,
    }


# --- text matching --------------------------------------------------------


def test_fold_strips_diacritics_and_case():
    assert fold("Nurniho") == "nurniho"
    assert fold("Roztylská") == "roztylska"
    assert fold("PRAHA 4 – Spořilov") == "praha 4 – sporilov"
    assert fold(None) == ""


def test_match_street_is_accent_and_case_insensitive():
    row = listing("a", address="Roztylská 15, Praha 4")
    assert match_street(row, "roztylska") is True
    assert match_street(row, "Roztylská") is True
    assert match_street(row, "ROZTYLSKA") is True


def test_match_street_also_looks_at_the_url_slug():
    """sreality bakes the street into its canonical URL, which for a listing
    with a thin address field is the only street signal available."""
    row = listing("a", address="", url="https://www.sreality.cz/detail/prodej/byt/2+kk/praha-sporilov-nurniho/1")
    assert match_street(row, "Nurniho") is True


def test_match_street_does_not_match_an_unrelated_street():
    row = listing("a", address="Roztylská 15, Praha 4", url="https://example.com/roztylska/1", description="")
    assert match_street(row, "Nurniho") is False


def test_match_street_ignores_an_empty_query():
    assert match_street(listing("a"), "") is False
    assert match_street(listing("a"), "   ") is False


def test_as_bool_reads_the_csv_literals():
    assert as_bool("True") is True
    assert as_bool("False") is False
    assert as_bool("") is False


def test_is_live_counts_not_yet_confirmed_missing_as_on_market():
    assert is_live({"status": "active"}) is True
    assert is_live({"status": "missing_1"}) is True
    assert is_live({"status": "removed"}) is False


# --- joining prices back in ----------------------------------------------


def test_latest_price_wins_across_months(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [listing("a")],
        [
            observation("a", "2026-02-01T10:00:00+00:00", "6000000"),
            observation("a", "2026-03-01T10:00:00+00:00", "5800000"),
        ],
    )
    latest = load_latest_prices(data_dir)
    assert latest["a"]["price"] == "5800000"


def test_enrich_attaches_price_and_tolerates_a_listing_with_none(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [listing("a"), listing("b")], [observation("a", "2026-03-01T10:00:00+00:00", "5000000")])
    rows = enrich([listing("a"), listing("b")], load_latest_prices(data_dir))
    by_id = {r["internal_id"]: r for r in rows}
    assert by_id["a"]["price"] == "5000000"
    assert by_id["b"]["price"] == ""


# --- export views ---------------------------------------------------------


def test_export_writes_all_four_views(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [
            listing("a"),
            listing("b", status="removed", priority_zone="False"),
            listing("c", priority_zone="False"),
        ],
        [observation("a", "2026-03-01T10:00:00+00:00", "5000000")],
    )
    stats = export(data_dir)

    assert stats == {"total": 3, "live": 2, "active": 2, "priority_zone": 1}
    csv_dir = data_dir / "csv"
    for name in ("aktivni_inzeraty.csv", "priority_zona.csv", "vse_vcetne_zmizelych.csv", "souhrn.csv"):
        assert (csv_dir / name).exists(), name

    with open(csv_dir / "aktivni_inzeraty.csv", newline="", encoding="utf-8") as f:
        live_rows = list(csv.DictReader(f))
    assert {r["internal_id"] for r in live_rows} == {"a", "c"}  # 'b' is removed
    assert live_rows[0]["price"] in {"5000000", ""}


def test_export_is_reproducible_and_idempotent(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [listing("a")], [observation("a", "2026-03-01T10:00:00+00:00", "1")])
    export(data_dir)
    first = (data_dir / "csv" / "aktivni_inzeraty.csv").read_bytes()
    export(data_dir)
    assert (data_dir / "csv" / "aktivni_inzeraty.csv").read_bytes() == first


def test_export_on_an_empty_dataset_does_not_crash(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [], [])
    stats = export(data_dir)
    assert stats["total"] == 0
    assert (data_dir / "csv" / "souhrn.csv").exists()


# --- the street filter ----------------------------------------------------


def test_filter_street_finds_the_listing_and_writes_its_folder(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [listing("a"), listing("b", address="Roztylská 15, Praha 4", url="https://example.com/roztylska/2")],
        [observation("a", "2026-03-01T10:00:00+00:00", "5000000")],
    )
    result = filter_street("Nurniho", data_dir)

    assert result["matched"] == 1
    out = result["out_dir"]
    assert (out / "inzeraty.csv").exists()
    assert (out / "cenova_historie.csv").exists()
    assert (out / "souhrn.txt").exists()

    with open(out / "inzeraty.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["internal_id"] for r in rows] == ["a"]
    assert rows[0]["price"] == "5000000"


def test_filter_street_includes_the_full_price_history(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [listing("a")],
        [
            observation("a", "2026-02-01T10:00:00+00:00", "6000000"),
            observation("a", "2026-03-01T10:00:00+00:00", "5500000"),
        ],
    )
    result = filter_street("Nurniho", data_dir)
    with open(result["out_dir"] / "cenova_historie.csv", newline="", encoding="utf-8") as f:
        history = list(csv.DictReader(f))
    assert [h["price"] for h in history] == ["6000000", "5500000"]


def test_filter_street_excludes_removed_listings_unless_asked(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [listing("a", status="removed")], [])
    assert filter_street("Nurniho", data_dir)["matched"] == 0
    assert filter_street("Nurniho", data_dir, include_removed=True)["matched"] == 1


def test_filter_street_respects_property_and_transaction_filters(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [listing("a", property_type="byt"), listing("b", property_type="dum", transaction_type="pronajem")],
        [],
    )
    assert filter_street("Nurniho", data_dir, property_type="byt")["matched"] == 1
    assert filter_street("Nurniho", data_dir, transaction_type="pronajem")["matched"] == 1


def test_filter_street_explains_an_empty_result_instead_of_just_showing_zero(tmp_path):
    """An empty result has several very different causes; the summary has to
    distinguish 'nothing for sale there' from 'the dataset is still empty'."""
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [listing("a", address="Roztylská 15", url="https://example.com/r/1")], [])
    result = filter_street("Nurniho", data_dir)
    assert result["matched"] == 0
    summary = result["summary"]
    assert "Žádný inzerát neodpovídá" in summary
    assert "Prohledáno inzerátů v datasetu: 1" in summary


def test_filter_street_suggests_the_street_the_user_meant(tmp_path):
    """A real miss this tool had: searching "Nurniho" returned nothing while
    the dataset held a listing on **Nurmiho** - one letter apart, in exactly
    the district the user cares about. Prefix matching slipped right past a
    typo in the middle of a word."""
    data_dir = tmp_path / "data"
    write_dataset(
        data_dir,
        [listing("a", address="Nurmiho 12, Hostivar, Praha", url="https://example.com/x/1")],
        [],
    )
    result = filter_street("Nurniho", data_dir)
    assert result["matched"] == 0
    assert "Nurmiho" in result["summary"]
    assert "překlep" in result["summary"]


def test_filter_street_substring_query_still_matches(tmp_path):
    data_dir = tmp_path / "data"
    write_dataset(data_dir, [listing("a", address="Nurnihova 5, Praha")], [])
    assert filter_street("Nurni", data_dir)["matched"] == 1


def test_street_names_strips_house_numbers(tmp_path):
    from tools.filter_street import street_names

    names = street_names([
        {"address": "Nurmiho 12, Hostivar, Praha"},
        {"address": "Nurmiho 1101/4, Praha"},
        {"address": "V Zeleném údolí 1302/11, Praha"},
    ])
    assert names["Nurmiho"] == 2
    assert names["V Zeleném údolí"] == 1


def test_slugify_produces_a_safe_folder_name():
    assert slugify("Nurniho") == "nurniho"
    assert slugify("Roztylská 15/2") == "roztylska-15-2"
    assert slugify("???") == "ulice"


# --- probe: finding the actual terms pages --------------------------------

from tools.probe_sources import _clause_density, _terms_links, _terms_url_strings


def test_terms_links_ignores_third_party_policy_pages():
    """The first probe run followed the homepage's reCAPTCHA notice all the
    way to policies.google.com and saved Google's terms as bezrealitky's."""
    html = (
        '<a href="https://policies.google.com/terms">smluvní podmínky</a>'
        '<a href="/informace/smluvni-podminky">Podmínky</a>'
    )
    found = _terms_links(html, "https://www.bezrealitky.cz")
    assert found == [("https://www.bezrealitky.cz/informace/smluvni-podminky", "podmínky")]


def test_terms_links_makes_relative_hrefs_absolute_and_dedupes():
    html = '<a href="/podminky">A</a><a href="podminky">B</a>'
    assert _terms_links(html, "https://x.cz/") == [("https://x.cz/podminky", "a")]


def test_root_relative_href_drops_the_current_page_path():
    """String concatenation appended the href to the page path and produced
    a 404, which is why the real bezrealitky terms were missed at first."""
    html = '<a href="/informace/obchodni-podminky">Obchodní podmínky</a>'
    found = _terms_links(html, "https://www.bezrealitky.cz/informace/smluvni-podminky")
    assert found[0][0] == "https://www.bezrealitky.cz/informace/obchodni-podminky"


def test_clause_density_separates_a_contract_from_a_table_of_contents():
    index_page = "Smluvní podmínky\nObchodní podmínky serveru\nZásady zpracování\n"
    contract = (
        "Do databáze je zakázáno jakkoli zasahovat. Je zakázáno vytěžovat obsah. "
        "Jste povinni dodržovat tyto podmínky. Vyhrazujeme si právo inzerát odstranit. "
        "Jsme oprávněni uplatnit smluvní pokutu."
    )
    assert _clause_density(index_page) == 0
    assert _clause_density(contract) >= 5


def test_terms_urls_are_found_when_they_are_page_data_rather_than_markup():
    """bezrealitky renders its terms index client-side: the contracts are
    routes inside __NEXT_DATA__, so an anchor-only scan saw only the footer."""
    html = '{"slug":"/informace/obchodni-podminky","title":"Obchodni podminky"}'
    found = _terms_url_strings(html, "https://www.bezrealitky.cz/informace/smluvni-podminky")
    assert found[0][0] == "https://www.bezrealitky.cz/informace/obchodni-podminky"


def test_terms_url_strings_ignores_unrelated_paths():
    assert _terms_url_strings('{"slug":"/informace/o-nas"}', "https://x.cz/") == []


def test_registrable_domain_lets_the_crawl_reach_a_portals_sibling_host():
    """Both portals keep their contracts off the www host - sreality on
    o-seznam.cz, bezrealitky as PDFs on api.bezrealitky.cz."""
    from tools.probe_sources import _registrable_domain

    assert _registrable_domain("https://api.bezrealitky.cz/page-file/x.pdf") == "bezrealitky.cz"
    assert _registrable_domain("https://www.bezrealitky.cz/informace") == "bezrealitky.cz"
    assert _registrable_domain("https://o-seznam.cz/napoveda") == "o-seznam.cz"
    assert _registrable_domain("https://www.sreality.cz/") != _registrable_domain("https://o-seznam.cz/")


# --- what the browsing views show ------------------------------------------

def test_the_views_do_not_carry_the_raw_address():
    """`address` is whichever shape the portal used - "Ke Slatinam, Dolni
    Mecholupy, Praha" next to "Rohacova, Praha 3 - Zizkov" - so a column of
    them sorts by the portal rather than by the street. The parsed fields are
    what a person reads."""
    from tools.export_csv import VIEW_FIELDS
    assert "address" not in VIEW_FIELDS
    for field in ("ulice", "cislo_popisne", "mestska_cast"):
        assert field in VIEW_FIELDS, field


def test_the_raw_address_is_still_kept_in_the_record():
    """Removing it from the record would break cross-source matching:
    dedup.py reads it through street_key() and locality_tokens(), and it is
    the evidence ulice / mestska_cast / obec are parsed from. A view is a
    view; the record has to keep its evidence."""
    from common.schema import LISTING_FIELDS
    assert "address" in LISTING_FIELDS

    import inspect
    from common import dedup
    source = inspect.getsource(dedup)
    assert 'get("address")' in source, (
        "dedup no longer reads the raw address; if that is deliberate this "
        "test should go, but it must be deliberate")


def test_a_house_number_never_appears_in_a_view_without_its_caveats():
    """The number is inferred from the address register, not published by any
    portal. Shown alone it reads as something the advert said."""
    from tools.export_csv import VIEW_FIELDS
    from common import ruian
    assert "cislo_popisne" in VIEW_FIELDS
    for field in ruian.MATCH_FIELDS:
        assert field in VIEW_FIELDS, (
            f"{field} is missing from the view, so the house number is shown "
            "with less than it needs to be read honestly")
