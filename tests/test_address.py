"""Breaking a portal's address into fields.

Every case here is a shape that appears in the collected data, with the count
it appears at. The three sources each say the same thing differently, and the
thin variants - a district where a street should be, an address that is only
"Praha" - are what a parser written against the common case gets wrong.
"""

import pytest

from common.address import DISTRICTS, parse, strip_diacritics


def test_diacritics_are_folded_to_ascii():
    assert strip_diacritics("Roháčova") == "Rohacova"
    assert strip_diacritics("Žižkov") == "Zizkov"
    assert strip_diacritics("Újezd nad Lesy") == "Ujezd nad Lesy"


def test_case_and_spacing_survive_folding():
    """Folded, not normalised: these are names, and a column of lowercase
    names is harder to read than one that kept its capitals."""
    assert strip_diacritics("Na Rokytce") == "Na Rokytce"


def test_nothing_folds_to_nothing():
    assert strip_diacritics(None) == ""
    assert strip_diacritics("") == ""


# --- the three shapes, at the counts they occur -----------------------------


def test_srealitys_three_parts():
    """2122 rows: street, district, city."""
    assert parse("Ke Slatinam, Dolni Mecholupy, Praha") == {
        "ulice": "Ke Slatinam", "cislo_popisne": "",
        "mestska_cast": "Dolni Mecholupy", "obec": "Praha"}


def test_idness_borough_dash_district():
    """6694 rows: street, then "Praha 3 - Zizkov"."""
    assert parse("Roháčova, Praha 3 - Žižkov") == {
        "ulice": "Rohacova", "cislo_popisne": "",
        "mestska_cast": "Zizkov", "obec": "Praha"}


def test_bezrealitkys_two_parts_have_no_district():
    """1737 rows: street and city, nothing finer. An empty district is the
    honest answer - inventing one from the street would be a guess."""
    assert parse("Štichova, Praha") == {
        "ulice": "Stichova", "cislo_popisne": "",
        "mestska_cast": "", "obec": "Praha"}


# --- the shapes that break a parser written for the common case -------------


def test_a_district_standing_where_a_street_would_be():
    """108 rows are "X, Praha" and 42 of those X are districts, not streets.
    Same shape, and only the vocabulary can tell them apart."""
    assert parse("Hostivar, Praha")["mestska_cast"] == "Hostivar"
    assert parse("Hostivar, Praha")["ulice"] == ""


def test_a_street_standing_in_the_same_place_stays_a_street():
    assert parse("Leopoldova, Praha")["ulice"] == "Leopoldova"
    assert parse("Leopoldova, Praha")["mestska_cast"] == ""


def test_an_address_with_no_street_at_all():
    """103 rows: the portal gave only a borough and district."""
    assert parse("Praha 2 - Vinohrady") == {
        "ulice": "", "cislo_popisne": "", "mestska_cast": "Vinohrady",
        "obec": "Praha"}


def test_an_address_that_is_only_the_city():
    assert parse("Praha") == {"ulice": "", "cislo_popisne": "",
                              "mestska_cast": "", "obec": "Praha"}


def test_a_borough_with_no_district_is_the_finest_locality_there_is():
    """71 rows end in "Praha 9" and name no district. Keeping the borough
    beats dropping it: it is coarser than a district, not nothing."""
    assert parse("Poděbradská, Praha 9") == {
        "ulice": "Podebradska", "cislo_popisne": "",
        "mestska_cast": "Praha 9", "obec": "Praha"}


def test_the_okres_tail_is_dropped():
    """5 rows end in "okres Praha", which says nothing the rest does not."""
    assert parse("Svitákova, Praha 5 - Stodůlky, okres Praha") == {
        "ulice": "Svitakova", "cislo_popisne": "",
        "mestska_cast": "Stodulky", "obec": "Praha"}


def test_somewhere_that_is_not_prague_keeps_its_own_name():
    """The dataset is Prague, but the portals return neighbouring towns too,
    and calling Jesenice a district of Prague would be wrong twice over."""
    assert parse("Cedrová, Jesenice") == {
        "ulice": "Cedrova", "cislo_popisne": "", "mestska_cast": "",
        "obec": "Jesenice"}


def test_a_borough_in_the_district_slot():
    """sreality sometimes writes "5 Kvetna, Praha 4, Praha"."""
    assert parse("5 Kvetna, Praha 4, Praha") == {
        "ulice": "5 Kvetna", "cislo_popisne": "",
        "mestska_cast": "Praha 4", "obec": "Praha"}


def test_an_empty_address_parses_to_empty_fields():
    for empty in (None, "", "   ", ",,"):
        assert parse(empty) == {"ulice": "", "cislo_popisne": "",
                                "mestska_cast": "", "obec": ""}


# --- the house number, which is not there -----------------------------------


def test_the_house_number_is_always_empty():
    """Not an oversight: no Czech portal here publishes one."""
    for address in ("Ke Slatinam, Dolni Mecholupy, Praha",
                    "Roháčova, Praha 3 - Žižkov", "Štichova, Praha"):
        assert parse(address)["cislo_popisne"] == ""


def test_a_number_in_a_street_name_stays_in_the_street_name():
    """The reason there is no digit-extraction rule. All 42 addresses in the
    data that contain a digit are streets named after dates."""
    assert parse("5. května, Praha 4 - Nusle")["ulice"] == "5. kvetna"
    assert parse("28. pluku, Praha 10 - Vršovice")["ulice"] == "28. pluku"
    assert parse("náměstí 14. října, Praha")["ulice"] == "namesti 14. rijna"
    assert parse("17. listopadu, Říčany")["ulice"] == "17. listopadu"


# --- the vocabulary ---------------------------------------------------------


def test_the_two_word_districts_survived_being_listed():
    """They are the ones a bare split() on the list would tear in half."""
    for name in ("cerny most", "dolni mecholupy", "stare mesto",
                 "ujezd nad lesy", "mala strana"):
        assert name in DISTRICTS


def test_a_borough_is_not_in_the_district_vocabulary():
    """"Praha 4" turns up in the district slot but is a borough; leaving it
    in the vocabulary would make the two indistinguishable."""
    assert "praha 4" not in DISTRICTS
