"""Break a portal's address string into fields, without diacritics.

The three sources say the same thing three ways:

    sreality      "Ke Slatinam, Dolni Mecholupy, Praha"   street, district, city
    idnes         "Rohacova, Praha 3 - Zizkov"            street, borough - district
    bezrealitky   "Stichova, Praha"                       street, city

and each of them has thinner variants: an address that is only "Praha", one
that names a district where a street should be, one that ends in "okres
Praha". Sorting a column by address therefore sorts by whichever shape the
portal happened to use, which is no order at all.

This parses all of them into four fields and folds the result to ASCII, so
"Rohacova" and "Roháčova" are the same string whichever portal said it.

WHY THERE IS NO HOUSE NUMBER

There is a `cislo_popisne` field and it is always empty. Czech property
portals do not publish house numbers - you get them from the agent - and a
search of all 10,943 addresses collected found 42 containing a digit, every
one of them part of a street NAME: "5. kvetna", "28. pluku", "17. listopadu",
"namesti 14. rijna". So a rule that pulled digits out would find no house
numbers and would wreck those streets, turning "5. kvetna" into the street
"kvetna" at number 5.

The field exists so that the day a source does supply one there is somewhere
to put it, and so that its emptiness is a stated fact about the sources
rather than a gap someone has to rediscover.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

# Prague's cadastral districts, as the portals name them - folded to ASCII.
# Taken from the data itself: every district-slot value the three sources have
# ever used. Needed because "Hostivar, Praha" and "Leopoldova, Praha" are the
# same shape and only a vocabulary can say which word is a street.
DISTRICTS = frozenset("""
bechovice benice bohnice branik brevnov bubenec cakovice chodov cimice
dablice dejvice dubec haje hloubetin hlubocepy hodkovicky holesovice
hostavice hostivar hradcany hrdlorezy jinonice josefov kamyk karlin kbely
klanovice kobylisy kolodeje kolovraty komorany kosire kralovice krc kunratice
kyje lahovice letnany lhotka liben liboc libus lipence lochkov lysolaje
malesice michle miskovice modrany motol nebusice nusle petrovice pisnice
pitkovice podoli prosek radlice radotin reporyje repy ruzyne satalice
seberov sedlec slivenec smichov sobin sterboholy stodulky strasnice
stresovice strizkov suchdol tocna trebonice treboradice troja uhrineves
veleslavin vinohrady vinor vokovice vrsovice vysehrad vysocany zabehlice
zbraslav zizkov zlicin
""".split()) | frozenset({
    # The two-word ones, which a bare .split() would tear in half.
    "cerny most", "dolni chabry", "dolni mecholupy", "dolni pocernice",
    "horni mecholupy", "horni pocernice", "mala strana", "nove mesto",
    "predni kopanina", "stare mesto", "velka chuchle", "nedvezi u rican",
    "ujezd nad lesy", "ujezd u pruhonic",
})


BOROUGH_RE = re.compile(r"^praha\s*\d+$")
OKRES_RE = re.compile(r"^okres\b")


def strip_diacritics(text: Optional[str]) -> str:
    """"Roháčova" -> "Rohacova". Case and spacing are left alone."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _key(text: str) -> str:
    """Lowercase ASCII, for comparing against DISTRICTS."""
    return re.sub(r"\s+", " ", strip_diacritics(text).lower()).strip()


def parse(raw: Optional[str]) -> dict:
    """{ulice, cislo_popisne, mestska_cast, obec}, all ASCII, all possibly "".

    Read right to left: the last part names the municipality, a part carrying
    " - " is a borough and its district, and the first part is the street
    unless the vocabulary says it is a district.
    """
    blank = {"ulice": "", "cislo_popisne": "", "mestska_cast": "", "obec": ""}
    if not raw or not raw.strip():
        return blank

    parts = [strip_diacritics(p).strip() for p in raw.split(",")]
    parts = [p for p in parts if p and not OKRES_RE.match(_key(p))]
    if not parts:
        return blank

    obec = ""
    mestska_cast = ""

    # "Praha 3 - Zizkov": the borough is the coarse half, the district the fine.
    for index, part in enumerate(parts):
        if " - " in part:
            borough, _, district = part.partition(" - ")
            parts[index] = borough.strip()
            mestska_cast = district.strip()

    last = parts[-1]
    if BOROUGH_RE.match(_key(last)):
        # "Podebradska, Praha 9" - the borough is the finest locality given.
        obec = "Praha"
        if not mestska_cast:
            mestska_cast = last
        parts = parts[:-1]
    else:
        obec = last
        parts = parts[:-1]

    # What is left before the municipality: a district, then a street.
    while parts and (BOROUGH_RE.match(_key(parts[-1]))
                     or _key(parts[-1]) in DISTRICTS):
        candidate = parts.pop()
        if not mestska_cast:
            mestska_cast = candidate

    ulice = parts[0] if parts else ""
    # "Hostivar, Praha" reaches here only if Hostivar was consumed above; a
    # lone street that IS the municipality name is not a street.
    if _key(ulice) == _key(obec):
        ulice = ""

    return {"ulice": ulice, "cislo_popisne": "", "mestska_cast": mestska_cast,
            "obec": obec}
