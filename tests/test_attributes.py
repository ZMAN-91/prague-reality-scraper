"""Construction, ownership, condition, charges and seller, from both portals,
in one vocabulary - and how they enter the dataset."""

from common import attributes
from common.schema import NormalizedListing


def test_bezrealitky_values_map_to_the_shared_vocabulary():
    got = attributes.from_bezrealitky({
        "ownership": "DRUZSTEVNI", "construction": "PANEL", "condition": "AFTER_RECONSTRUCTION",
        "penb": "c", "equipped": "CASTECNE", "charges": 4500, "deposit": 40000,
        "isDiscounted": True, "originalPrice": 26000, "price": 24000, "type": "UNDEFINED"})
    assert got == {"vlastnictvi": "druzstevni", "konstrukce": "panel", "stav": "po rekonstrukci",
                   "penb": "C", "vybaveni": "castecne", "poplatky_kc": 4500, "kauce_kc": 40000,
                   "sleva_portal": "ano", "puvodni_cena": 26000, "prodejce": "soukromy", "rk_id": ""}


def test_not_chosen_is_empty_not_no():
    got = attributes.from_bezrealitky({"ownership": "UNDEFINED", "construction": "UNDEFINED",
                                       "penb": None, "charges": 0, "deposit": 0})
    assert got["vlastnictvi"] == got["konstrukce"] == got["penb"] == ""
    assert got["poplatky_kc"] == got["kauce_kc"] == "", "zero is the portal's 'not given'"
    sr = attributes.from_sreality({"energy_efficiency_rating_cb": {"name": "- vyber třídu", "value": 0},
                                   "furnished": {"name": "- vyber vybavení", "value": 0}})
    assert sr["penb"] == sr["vybaveni"] == ""


def test_bezrealitky_companies_are_neither_owner_nor_agency():
    assert attributes.from_bezrealitky({"type": "TRIGEMA"})["prodejce"] == "firma"
    assert attributes.from_bezrealitky({"type": "TRIGEMA"})["rk_id"] == "trigema"
    assert attributes.from_bezrealitky({"type": "MANUAL_BZR_RK_PRODEJ"})["prodejce"] == "rk"
    assert attributes.from_bezrealitky({"type": "DOKONALY_NAJEMNIK"})["prodejce"] == "soukromy"


def test_sreality_detail_and_index_rows():
    detail = attributes.from_sreality({
        "ownership": {"name": "Osobní", "value": 1}, "building_type": {"name": "Cihlová", "value": 2},
        "building_condition": {"name": "Před rekonstrukcí", "value": 8},
        "energy_efficiency_rating_cb": {"name": "G - Mimořádně nehospodárná", "value": 7},
        "furnished": {"name": "Částečně", "value": 3}, "cost_of_living": "5500",
        "discount_show": True, "price_summary_old_czk": 10490000.0, "price_summary_czk": 9990000,
        "premise": {"id": 9625}})
    assert detail == {"vlastnictvi": "osobni", "konstrukce": "cihla", "stav": "pred rekonstrukci",
                      "penb": "G", "vybaveni": "castecne", "poplatky_kc": 5500, "kauce_kc": "",
                      "sleva_portal": "ano", "puvodni_cena": 10490000, "prodejce": "rk", "rk_id": "9625"}
    index = attributes.from_sreality({"discount_show": False, "premise_id": None, "price_czk": 1})
    assert index["prodejce"] == "soukromy" and index["sleva_portal"] == "ne"
    assert index["vlastnictvi"] == "", "an index row does not say, and must not erase"


def _listing(sid, **attrs):
    listing = NormalizedListing(source="sreality", source_id=sid, url=f"https://x/{sid}",
                                property_type="byt", transaction_type="prodej",
                                disposition="2+kk", area_m2=55.0, lat=50.03, lon=14.45, price=5_000_000)
    listing.attributes = attrs
    return listing


def _merge(listing, listings, last_obs, when):
    from run import merge_source
    from tests.test_run import ALL_SCOPES
    return merge_source("sreality", [listing], [], listings, last_obs, when, [], ALL_SCOPES)


def test_first_fill_is_not_an_edit_a_change_is_and_a_badge_is_not():
    listings, last_obs = {}, {}
    _merge(_listing("1"), listings, last_obs, "2026-10-05T10:00:00+00:00")
    row = next(iter(listings.values()))
    assert row["konstrukce"] == "" and row["prodejce"] == ""

    _, _, changes = _merge(_listing("1", konstrukce="cihla", prodejce="rk", rk_id="9"),
                           listings, last_obs, "2026-10-05T11:00:00+00:00")
    assert changes == [], "learning a field for the first time is not an edit"
    assert row["konstrukce"] == "cihla" and row["rk_id"] == "9"

    _, _, changes = _merge(_listing("1", konstrukce="panel", sleva_portal="ano"),
                           listings, last_obs, "2026-10-05T12:00:00+00:00")
    assert [c["field"] for c in changes] == ["konstrukce"], "the badge is not logged"
    assert row["sleva_portal"] == "ano"

    _merge(_listing("1"), listings, last_obs, "2026-10-05T13:00:00+00:00")
    assert row["konstrukce"] == "panel" and row["rk_id"] == "9", "not said is not erased"
