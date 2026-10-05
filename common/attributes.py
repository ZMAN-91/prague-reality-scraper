"""What a listing is built of and who sells it, from what the portals send.

Added 2026-10-05 after the inventory showed these arriving with every advert
and being thrown away. They are among the strongest things a price depends
on - a cooperative flat (druzstevni) sells well below a freehold one, a
panel block below brick - and the rent a tenant actually pays includes the
charges. All of them can be read back from the raw archive, so history is
filled in by tools/backfill_attributes.py rather than starting today.

One vocabulary for every portal, lower case without diacritics like the
other text columns, so a filter on `konstrukce == "panel"` means the same
thing whichever portal the row came from:

    vlastnictvi   osobni | druzstevni | obecni | ostatni
    konstrukce    cihla | panel | smisena | skelet | drevostavba | kamen |
                  montovana | modularni
    stav          novostavba | velmi dobry | dobry | po rekonstrukci |
                  castecne po rekonstrukci | pred rekonstrukci |
                  v rekonstrukci | ve vystavbe | projekt | spatny | k demolici
    penb          A..G
    vybaveni      ano | castecne | ne
    poplatky_kc   monthly charges on top of the price, where the portal gives
                  them (rent: services and energy; sale: what the advert says)
    kauce_kc      the deposit a rent asks for
    sleva_portal  ano | ne - whether the portal itself shows the price as cut,
                  which is the only trace of a cut made before collection
                  began
    puvodni_cena  the price before that cut, where the portal gives it
    prodejce      rk (an agency) | soukromy (an owner) | firma (a developer
                  or landlord company selling directly)
    rk_id         which agency or company, as the portal identifies it, so
                  one agency's re-posts of one flat can be told apart from
                  two flats

Empty means the portal did not say, never "no". A value the portal leaves at
its own "not chosen" option ("- vyber tridu", UNDEFINED) is empty too.

Which fields count as edits: a seller changing the stated construction or the
charges is an edit of the advert and goes to the change log like any other.
The portal's discount flag, the original price and the agency id describe the
listing's circumstances, not its content, and are kept current silently.
"""

from __future__ import annotations

import unicodedata
from typing import Optional

#: Columns a seller edits; a change is logged in data/changes/.
EDITABLE_FIELDS = (
    "vlastnictvi", "konstrukce", "stav", "penb", "vybaveni",
    "poplatky_kc", "kauce_kc",
)
#: Columns kept current without logging a change.
CIRCUMSTANCE_FIELDS = ("sleva_portal", "puvodni_cena", "prodejce", "rk_id")
FIELDS = EDITABLE_FIELDS + CIRCUMSTANCE_FIELDS


def _fold(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in text if not unicodedata.combining(c)).strip().lower()


def _number(value) -> Optional[int]:
    """A positive whole number of korun, or None. Zero is the portals' "not
    given" more often than it is a real zero."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _named(value) -> str:
    """sreality wraps enums as {"name": "Osobní", "value": 1}."""
    if isinstance(value, dict):
        return value.get("name") or ""
    return value or ""


# --- bezrealitky -------------------------------------------------------------

_BZ_OWNERSHIP = {"OSOBNI": "osobni", "DRUZSTEVNI": "druzstevni",
                 "OBECNI": "obecni", "STATNI": "obecni", "OSTATNI": "ostatni"}
_BZ_CONSTRUCTION = {"BRICK": "cihla", "PANEL": "panel", "MIXED": "smisena",
                    "SKELET": "skelet", "WOOD": "drevostavba", "STONE": "kamen",
                    "PREFAB": "montovana"}
_BZ_CONDITION = {"NEW": "novostavba", "VERY_GOOD": "velmi dobry", "GOOD": "dobry",
                 "AFTER_RECONSTRUCTION": "po rekonstrukci",
                 "AFTER_PARTIAL_RECONSTRUCTION": "castecne po rekonstrukci",
                 "BEFORE_RECONSTRUCTION": "pred rekonstrukci",
                 "IN_RECONSTRUCTION": "v rekonstrukci",
                 "CONSTRUCTION": "ve vystavbe", "PROJECT": "projekt", "BAD": "spatny"}
_BZ_EQUIPPED = {"VYBAVENY": "ano", "CASTECNE": "castecne", "NEVYBAVENY": "ne"}
#: bezrealitky is an owners' portal, but it also carries adverts placed
#: through its own landlord services and by named companies. Its own
#: services are still the owner selling.
_BZ_OWN_SERVICES = {"", "UNDEFINED", "DOKONALY_NAJEMNIK", "STALY_NAJEM",
                    "BZR_COMFORT", "MANUAL_BZR_SPRAVCE_PRONAJEM"}
_BZ_AGENCY = {"MANUAL_BZR_RK_PRODEJ"}


def from_bezrealitky(advert: dict) -> dict:
    out = {
        "vlastnictvi": _BZ_OWNERSHIP.get(str(advert.get("ownership") or "").upper(), ""),
        "konstrukce": _BZ_CONSTRUCTION.get(str(advert.get("construction") or "").upper(), ""),
        "stav": _BZ_CONDITION.get(str(advert.get("condition") or "").upper(), ""),
        "vybaveni": _BZ_EQUIPPED.get(str(advert.get("equipped") or "").upper(), ""),
    }
    penb = str(advert.get("penb") or "").strip().upper()
    out["penb"] = penb if penb in tuple("ABCDEFG") else ""

    charges = _number(advert.get("charges"))
    if charges is None:
        parts = [_number(advert.get(k)) for k in ("serviceCharges", "utilityCharges")]
        charges = sum(p for p in parts if p) or None
    out["poplatky_kc"] = charges or ""
    out["kauce_kc"] = _number(advert.get("deposit")) or ""

    discounted = advert.get("isDiscounted")
    out["sleva_portal"] = "ano" if discounted is True else "ne" if discounted is False else ""
    original, price = _number(advert.get("originalPrice")), _number(advert.get("price"))
    out["puvodni_cena"] = original if (discounted and original and price and original > price) else ""

    kind = str(advert.get("type") or "").upper()
    if kind in _BZ_OWN_SERVICES:
        out["prodejce"], out["rk_id"] = "soukromy", ""
    elif kind in _BZ_AGENCY:
        out["prodejce"], out["rk_id"] = "rk", ""
    else:
        out["prodejce"], out["rk_id"] = "firma", kind.lower()
    return out


# --- sreality ----------------------------------------------------------------

_SR_OWNERSHIP = {"osobni": "osobni", "druzstevni": "druzstevni",
                 "statni/obecni": "obecni"}
_SR_CONSTRUCTION = {"cihlova": "cihla", "panelova": "panel", "smisena": "smisena",
                    "skeletova": "skelet", "drevostavba": "drevostavba",
                    "kamenna": "kamen", "montovana": "montovana", "modularni": "modularni"}
_SR_CONDITION = {"novostavba": "novostavba", "velmi dobry": "velmi dobry",
                 "dobry": "dobry", "po rekonstrukci": "po rekonstrukci",
                 "pred rekonstrukci": "pred rekonstrukci",
                 "v rekonstrukci": "v rekonstrukci", "ve vystavbe": "ve vystavbe",
                 "projekt": "projekt", "spatny": "spatny", "k demolici": "k demolici"}
_SR_FURNISHED = {"ano": "ano", "castecne": "castecne", "ne": "ne"}


def from_sreality(estate: dict) -> dict:
    """From an index row or a detail record - the same object, the detail
    carrying more. Fields an index row lacks come out empty, and an empty
    value never overwrites a stored one (run.merge_source), so a later index
    sighting does not erase what a detail said."""
    out = {
        "vlastnictvi": _SR_OWNERSHIP.get(_fold(_named(estate.get("ownership"))), ""),
        "konstrukce": _SR_CONSTRUCTION.get(_fold(_named(estate.get("building_type"))), ""),
        "stav": _SR_CONDITION.get(_fold(_named(estate.get("building_condition"))), ""),
        "vybaveni": _SR_FURNISHED.get(_fold(_named(estate.get("furnished"))), ""),
    }
    rating = _fold(_named(estate.get("energy_efficiency_rating_cb")))[:1].upper()
    out["penb"] = rating if rating in tuple("ABCDEFG") else ""
    out["poplatky_kc"] = _number(estate.get("cost_of_living")) or ""
    out["kauce_kc"] = ""

    discount = estate.get("discount_show")
    out["sleva_portal"] = "ano" if discount is True else "ne" if discount is False else ""
    original = _number(estate.get("price_summary_old_czk"))
    price = _number(estate.get("price_summary_czk") or estate.get("price_czk"))
    out["puvodni_cena"] = original if (original and price and original > price) else ""

    premise = estate.get("premise")
    premise_id = estate.get("premise_id") or (premise.get("id") if isinstance(premise, dict) else None)
    if premise_id:
        out["prodejce"], out["rk_id"] = "rk", str(premise_id)
    elif "premise_id" in estate or "premise" in estate:
        # The field is there and empty: no agency behind the advert.
        out["prodejce"], out["rk_id"] = "soukromy", ""
    else:
        out["prodejce"], out["rk_id"] = "", ""
    return out
