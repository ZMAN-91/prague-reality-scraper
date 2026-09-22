"""Filling in the district, and not then marking its own homework.

2,061 of 13,408 rows carried no mestska_cast at all, and 1,866 of those were
every single bezrealitky listing - that portal does not publish one. For 1,852
of the 2,061 the answer was already in hand: they had been matched to a
building in the address register, and the register says which district that
building is in.

The trap is the second half. tools/quality.py watches whether the register
agrees with the portal about the district, and that check is the only thing
that would notice a broken coordinate conversion - every match would still be
produced, still at a plausible distance, and still look normal. If a district
the register supplied were then checked against the register, it would agree
by construction and walk the figure to 100%: the watchdog would be retired by
being fed its own output.
"""

from tools import backfill_cislo, quality


def row(**overrides):
    base = {
        "internal_id": "a", "source": "bezrealitky", "source_id": "a",
        "property_type": "byt", "transaction_type": "prodej",
        "lat": "50.05", "lon": "14.45", "ulice": "Nurmiho",
        "mestska_cast": "", "mestska_cast_zdroj": "", "obec": "Praha",
        "cislo_zdroj": "ruian", "psc": "10000",
    }
    base.update(overrides)
    return base


REGISTER = {"cast_obce": "Hradčany", "obvod": "Praha 1", "mestska_cast": "Praha 1"}


class FakeIndex:
    def __init__(self, by_street):
        self.by_street = by_street


# --- filling ----------------------------------------------------------------

def test_a_listing_with_no_district_gets_the_registers():
    r = row()
    stats = {}
    backfill_cislo._fill_district(r, REGISTER, __import__("collections").Counter())
    assert r["mestska_cast"] == "Hradcany", "diacritics must come off"
    assert r["mestska_cast_zdroj"] == "ruian"


def test_a_district_the_portal_stated_is_never_overwritten():
    """Even when the register disagrees. That disagreement is the finding
    the cross-check exists to report; overwriting it deletes the finding."""
    from collections import Counter
    r = row(mestska_cast="Nove Mesto", mestska_cast_zdroj="portal")
    backfill_cislo._fill_district(r, REGISTER, Counter())
    assert r["mestska_cast"] == "Nove Mesto"
    assert r["mestska_cast_zdroj"] == "portal"


def test_a_street_in_one_district_only_can_supply_it_without_a_match():
    from collections import Counter
    index = FakeIndex({"nurmiho": [{"cast_obce": "Hostivař"},
                                   {"cast_obce": "Hostivař"}]})
    r = row()
    backfill_cislo._fill_district_from_street(r, index, Counter())
    assert r["mestska_cast"] == "Hostivar"
    assert r["mestska_cast_zdroj"] == "ulice"


def test_a_listing_outside_prague_gets_no_prague_district():
    """The one this caught before it shipped. The register loaded here is
    Prague's, so a street name it knows means "Prague has a street by that
    name", not "this flat is on it". bezrealitky sweeps the ring of towns
    around Prague too, and street names repeat out there: measured before
    this guard, 36 of the 42 districts the street fallback supplied were
    wrong - Zitna in Hostivice placed in Nove Mesto, Riegrova in Cernosice
    in Klanovice, Kralupska in Brandys nad Labem in Ruzyne."""
    from collections import Counter
    index = FakeIndex({"zitna": [{"cast_obce": "Nové Město"}]})
    r = row(ulice="Zitna", obec="Hostivice")
    backfill_cislo._fill_district_from_street(r, index, Counter())
    assert r["mestska_cast"] == ""

    in_prague = row(ulice="Zitna", obec="Praha")
    backfill_cislo._fill_district_from_street(in_prague, index, Counter())
    assert in_prague["mestska_cast"] == "Nove Mesto"


def test_a_street_crossing_districts_supplies_nothing():
    """Evropska runs through four. A guessed district would land in the same
    column as a stated one with no way to tell them apart afterwards."""
    from collections import Counter
    index = FakeIndex({"nurmiho": [{"cast_obce": "Hostivař"},
                                   {"cast_obce": "Strašnice"}]})
    r = row()
    backfill_cislo._fill_district_from_street(r, index, Counter())
    assert r["mestska_cast"] == ""
    assert r["mestska_cast_zdroj"] == ""


# --- and not grading its own answers ----------------------------------------

def test_the_cross_check_ignores_a_district_it_supplied_itself():
    from collections import Counter
    districts = Counter()
    backfill_cislo._cross_check(
        row(mestska_cast="Hradcany", mestska_cast_zdroj="ruian"),
        REGISTER, districts)
    assert districts["agree"] == 0, "the register was graded against itself"
    assert districts["not the portal's district"] == 1


def test_the_cross_check_still_grades_what_the_portal_said():
    from collections import Counter
    districts = Counter()
    backfill_cislo._cross_check(
        row(mestska_cast="Hradcany", mestska_cast_zdroj="portal"),
        REGISTER, districts)
    assert districts["agree"] == 1


def test_a_row_from_before_the_column_existed_still_counts():
    """Every row already on disk has no mestska_cast_zdroj. Treating that as
    "not the portal's" would silently empty the cross-check the day this
    shipped."""
    from collections import Counter
    districts = Counter()
    stated = row(mestska_cast="Hradcany")
    del stated["mestska_cast_zdroj"]
    backfill_cislo._cross_check(stated, REGISTER, districts)
    assert districts["agree"] == 1


def test_quality_measures_agreement_only_over_portal_districts():
    """The same guard, in the place where the number becomes an exit code."""
    class Index:
        def match_detail(self, lat, lon, ulice):
            return ({}, REGISTER)

    supplied = {f"r{n}": row(internal_id=f"r{n}", mestska_cast="Hradcany",
                             mestska_cast_zdroj="ruian") for n in range(50)}
    measured = quality.measure(supplied, Index())
    assert "district_agreement_pct" not in measured, \
        "50 rows the register supplied were counted as 50 agreements"

    stated = {f"s{n}": row(internal_id=f"s{n}", mestska_cast="Hradcany",
                           mestska_cast_zdroj="portal") for n in range(50)}
    measured = quality.measure(stated, Index())
    assert measured["district_agreement_pct"] == 100.0
    assert measured["district_checked"] == 50


# --- the street column holds a street and nothing else -----------------------

def test_a_house_number_does_not_end_up_in_the_street_column():
    """ulice and cislo_popisne are separate columns on purpose: one fact per
    cell, or neither sorts. No portal in this dataset currently puts the
    number in that slot - measured over all 13,408 stored rows, this changes
    none of them - so this exists for the day one starts."""
    from common.address import parse
    assert parse("Nurmiho 1101/4, Praha 4 - Sporilov, Praha")["ulice"] == "Nurmiho"
    assert parse("Roztylska 15, Praha 4")["ulice"] == "Roztylska"
    assert parse("Nurmiho 4a, Praha")["ulice"] == "Nurmiho"


def test_a_street_named_after_a_date_keeps_its_number():
    """62 stored rows are on one. Only a TRAILING run of digits goes."""
    from common.address import parse
    assert parse("28. pluku, Praha 10 - Vrsovice")["ulice"] == "28. pluku"
    assert parse("5. maje, Praha 5")["ulice"] == "5. maje"
    assert parse("namesti 14. rijna, Praha")["ulice"] == "namesti 14. rijna"


# --- and the run says what it did -------------------------------------------

def test_a_row_that_only_gained_a_district_counts_as_changed():
    """The summary read "0 rows would change" on the run that gave 1,858
    listings the district they had never had, because only the house-number
    fields were counted. An --apply that reports nothing changed is one
    nobody runs twice."""
    class Index:
        by_street = {}

        def match_detail(self, lat, lon, ulice):
            return ({"cislo_popisne": "7", "cislo_orientacni": "2",
                     "cislo_typ": "c.p.", "psc": "10000",
                     "cislo_zdroj": "ruian", "cislo_vzdalenost_m": "4.0",
                     "cislo_kandidatu": "1"}, REGISTER)

    # Everything the match would give it, it already has: the only thing
    # left to gain is the district.
    already_numbered = row(cislo_popisne="7", cislo_orientacni="2",
                           cislo_typ="c.p.", psc="10000",
                           cislo_zdroj="ruian", cislo_vzdalenost_m="4.0",
                           cislo_kandidatu="1")
    rows = {"a": already_numbered}
    changed, _stats = backfill_cislo.backfill(rows, Index())
    assert already_numbered["mestska_cast"] == "Hradcany"
    assert changed == 1, "the district it gained was not counted"


def test_a_row_that_gained_nothing_counts_as_unchanged():
    """Or every run reports the whole dataset as changed and the number
    stops meaning anything."""
    class Index:
        by_street = {}

        def match_detail(self, lat, lon, ulice):
            return None

    settled = row(mestska_cast="Nove Mesto", mestska_cast_zdroj="portal",
                  cislo_popisne="", cislo_orientacni="", cislo_typ="",
                  psc="", cislo_zdroj="", cislo_vzdalenost_m="",
                  cislo_kandidatu="")
    changed, _stats = backfill_cislo.backfill({"a": settled}, Index())
    assert changed == 0
