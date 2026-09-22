"""sreality.cz scraper - verified against the live API.

## Status

Unlike earlier revisions of this file, the shapes below are no longer
inferred: a diagnostic run from CI (tools/probe_sources.py) confirmed them
against real responses, and the scraper has since collected real listings.

Confirmed live:
- `https://www.sreality.cz/api/v1/estates/search` works and answers
  `{"pagination": {"limit","offset","total"}, "results": [...]}`. The old
  `/api/cs/v2/estates` this project originally targeted is **404 - removed**
  (sreality was rebuilt on Next.js).
- `locality_district_id=47` is Praha: a probe for byty/prodej reported
  ~5,500 listings, and a full walk of all twelve slices found ~12,450.
- Detail lives at `/api/v1/estates/{id}`, wrapped as
  `{"result": {...}, "status_code": ...}`.
- Enum fields arrive as `{"name", "value"}` objects with `value == 0`
  meaning "unspecified"; the estate's id field is `hash_id`.

**Index rows are rich, but not in the obvious fields.** They carry
`locality` (with GPS), `category_sub_cb`, `advert_name`, `price_czk` and
`price_czk_m2` - not merely an id and a price, as this module first assumed.

They do NOT carry `usable_area`: a live row on 2026-09-21 had 36 keys and
that was not among them. The floor area is in the title ("Prodej bytu 2+kk
80 m2") and implied by price / price-per-m2, and `_area` reads both. This
paragraph claimed `usable_area` for a long time and the claim was never
true; believing it cost a city-wide pairing run that matched nothing,
because every candidate was compared against an area of None. That assumption cost one detail request per
listing (~12,000 to cover Prague); `parse_estate` now reads the index row
directly and `run.py` only falls back to the detail endpoint for rows that
are genuinely missing GPS/area/disposition. Same data, roughly three orders
of magnitude fewer requests - which matters most precisely because
sreality's robots.txt does not invite this traffic at all (see below).

## robots.txt

`www.sreality.cz/robots.txt` is 856 lines and 19 user-agent blocks.
Eighteen are named search engines with `Allow: /`. The block that applies to
everything else - including this project - is `Disallow: /`, the whole site.
There is no robots-compliant automated route to this data. That is handled
as an explicit, default-off, per-host policy rather than silently: see
`common/net.ROBOTS_OVERRIDE_HOSTS`, the single commented line in
.github/workflows/scrape.yml that sets it, and docs/robots/ for the verbatim
file.

## Still not verified

- `locality_district_id=56`/`57` (Praha-východ / Praha-západ, i.e. the
  "těsné okolí" the brief asks for) are corroborated by a second independent
  source using the same numbering, but were not confirmed by a live probe.
  If they are wrong, the affected slices simply error and are reported;
  because absence-marking is scoped per category, that no longer disables
  removal detection for the rest.
- Whether an honest self-identifying User-Agent is throttled harder than a
  browser-like one. This project keeps the honest UA per the brief.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

import requests

from common import net, interruptions
from common.budget import Budget
from common.geo import is_in_target_area, is_priority_zone
from common.schema import NormalizedListing, safe_float, safe_int

SOURCE_NAME = "sreality"

INDEX_URL = "https://www.sreality.cz/api/v1/estates/search"
DETAIL_URL_TMPL = "https://www.sreality.cz/api/v1/estates/{id}"
DETAIL_PAGE_BASE = "https://www.sreality.cz/detail"

COUNTRY_ID = 112  # Czech Republic, locality_country_id

CATEGORY_MAIN = {"byt": 1, "dum": 2}
CATEGORY_TYPE = {"prodej": 1, "pronajem": 2}

# sreality's own district numbering (locality_district_id) - see "What's
# concretely evidenced" above for the confidence level of each.
DISTRICT_PRAHA = 47
DISTRICT_PRAHA_VYCHOD = 56
DISTRICT_PRAHA_ZAPAD = 57
DISTRICT_IDS: tuple[int, ...] = (DISTRICT_PRAHA, DISTRICT_PRAHA_VYCHOD, DISTRICT_PRAHA_ZAPAD)

# The watched area, as the postal districts that hold it.
#
# 50xx is a second, finer id space in the same locality_district_id
# parameter: 47 is the okres Praha, 5004 and 5010 are the postal districts
# Praha 4 and Praha 10 inside it. Both confirmed by the localities they
# return, not by the request succeeding:
#
#   5004 -> 986 listings: Nusle, Chodov, Branik, Modrany, Krc, Zabehlice
#   5010 -> 793 listings: Vrsovice, Horni Mecholupy, Strasnice, Petrovice
#   (unfiltered: 5554)
#
# These are POSTAL districts, the same division iDNES uses - 5004 returns
# Chodov, which is city district Praha 11, and 5010 returns Horni Mecholupy
# and Petrovice, which are Praha 15. So the watched area needs the same two
# numbers on both portals, and scrapers/idnes.AREA_BRANCHES holds the other
# half of the pair. sreality's autocomplete calls Praha 10 a "mestska cast",
# which is true of its `quarter` entity and not of this space; taking that
# label at face value would have split the two sources apart for no reason.
#
# Praha 11 and Praha 15 have no id here at all, which is the same fact from
# the other side: they are city districts, not postal ones.
#
# The id had to be read off sreality's own search page. locality_district_id
# is the parameter this scraper already sends for Prague; five rounds of
# guessing its value failed silently, because this API answers a wrong id
# rather than refusing it - locality_district_id=0 returned all 20,135
# listings in the country. A wrong value and a missing feature look
# identical here.
DISTRICT_PRAHA_4 = 5004
DISTRICT_PRAHA_10 = 5010
AREA_DISTRICT_IDS: tuple[int, ...] = (DISTRICT_PRAHA_4, DISTRICT_PRAHA_10)

PER_PAGE = 500
# 500 * 200 = 100k rows per (category, district) combo - Praha alone is
# ~5k per category per the reference source's own comment, so this is a
# generous safety ceiling, not an expected depth.
MAX_PAGES_SAFETY = 200


# --- URL construction (ported from the reference's sreality_url.py) -------
#
# category_type_cb.value -> URL segment.
TYPE_SLUG: dict[int, str] = {1: "prodej", 2: "pronajem", 3: "drazby", 4: "podily"}
# category_main_cb.value -> URL segment.
MAIN_SLUG: dict[int, str] = {1: "byt", 2: "dum"}
# category_sub_cb.value -> (main segment, sub segment). Byt/dum subset only
# (this project's scope) - see module docstring for provenance.
SUB_SLUG: dict[int, tuple[str, str]] = {
    2: ("byt", "1+kk"), 3: ("byt", "1+1"), 4: ("byt", "2+kk"), 5: ("byt", "2+1"),
    6: ("byt", "3+kk"), 7: ("byt", "3+1"), 8: ("byt", "4+kk"), 9: ("byt", "4+1"),
    10: ("byt", "5+kk"), 11: ("byt", "5+1"), 12: ("byt", "6-a-vice"),
    16: ("byt", "atypicky"), 47: ("byt", "pokoj"),
    33: ("dum", "chata"), 35: ("dum", "pamatka"), 37: ("dum", "rodinny"),
    39: ("dum", "vila"), 40: ("dum", "na-klic"), 43: ("dum", "chalupa"),
    44: ("dum", "zemedelska-usedlost"), 54: ("dum", "vicegeneracni-dum"),
}

_DISPOSITION_RE = re.compile(r"\b(\d\+(?:kk|\d))\b", re.IGNORECASE)


def _cb_value(obj) -> Optional[int]:
    """Integer enum code from a {"name", "value"} object; 0 ("not
    specified") -> None. Matches sreality's own convention exactly."""
    if isinstance(obj, dict):
        v = obj.get("value")
        if isinstance(v, int) and not isinstance(v, bool) and v != 0:
            return v
    return None


def _seo(value) -> Optional[str]:
    return value.strip() or None if isinstance(value, str) else None


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "nemovitost"


def _build_source_url(raw: dict) -> Optional[str]:
    """The exact canonical detail-page URL, assembled the same way sreality's
    own frontend does (see module docstring). Returns None if any required
    part is missing/unmapped - callers fall back to `_fallback_url`."""
    listing_id = safe_int(raw.get("hash_id"))
    if not listing_id:
        return None
    type_slug = TYPE_SLUG.get(_cb_value(raw.get("category_type_cb")))
    main_slug = MAIN_SLUG.get(_cb_value(raw.get("category_main_cb")))
    if not type_slug or not main_slug:
        return None
    pair = SUB_SLUG.get(_cb_value(raw.get("category_sub_cb")))
    if pair is None or pair[0] != main_slug:
        return None
    sub_slug = pair[1]
    loc = raw.get("locality") or {}
    city = _seo(loc.get("city_seo_name"))
    if not city:
        return None
    citypart = _seo(loc.get("citypart_seo_name"))
    street = _seo(loc.get("street_seo_name"))
    locality_slug = f"{city}-{citypart or city}-{street or ''}"
    return f"{DETAIL_PAGE_BASE}/{type_slug}/{main_slug}/{sub_slug}/{locality_slug}/{listing_id}"


def _fallback_url(property_type: str, transaction_type: str, address_text: Optional[str], source_id: str) -> str:
    """Used only when `_build_source_url` can't assemble the precise URL
    (e.g. sreality's own data is missing a mapped category_sub_cb) - a
    same-shaped but locality-only-approximate link. May occasionally 404;
    logged nowhere because it's not an error on this project's side, just a
    rare data gap on sreality's - see module docstring."""
    type_slug = TYPE_SLUG.get(CATEGORY_TYPE.get(transaction_type, 1), "prodej")
    sub_slug = "atypicky" if property_type == "byt" else "rodinny"
    slug = _slugify(address_text) if address_text else "nemovitost"
    return f"{DETAIL_PAGE_BASE}/{type_slug}/{property_type}/{sub_slug}/{slug}-{slug}-/{source_id}"


def _disposition(raw: dict) -> Optional[str]:
    sub = raw.get("category_sub_cb") or {}
    name = sub.get("name") if isinstance(sub, dict) else None
    for source in (name, raw.get("advert_name")):
        if isinstance(source, str):
            m = _DISPOSITION_RE.search(source)
            if m:
                return m.group(1).lower()
    return None


def _unwrap_estate(payload: dict) -> dict:
    """The detail endpoint wraps the estate as `{result, status_code,
    status_message}`; unwrap it. Tolerates a flat payload too."""
    if "category_main_cb" in payload:
        return payload
    for key in ("result", "estate", "data"):
        inner = payload.get(key)
        if isinstance(inner, dict) and "category_main_cb" in inner:
            return inner
    return payload


# --- Index walk (id + price only - see module docstring) -------------------


def _extract_index_id(raw: dict) -> Optional[str]:
    for key in ("hash_id", "id"):
        v = raw.get(key)
        if v is not None:
            return str(v)
    return None


def _extract_index_price(raw: dict) -> Optional[int]:
    for key in ("price_summary_czk", "price_czk", "price"):
        n = safe_int(raw.get(key))
        if n:
            return n
    return None


_AREA_IN_NAME_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*m(?:²|2|\^2)")


def _area_from_name(raw: dict) -> Optional[float]:
    """The floor area out of "Prodej bytu 2+kk 80 m\u00b2".

    Index rows have no `usable_area` at all - confirmed against the live API
    on 2026-09-21, whose row carried 36 keys and not that one - but the title
    states it, exactly as it states the disposition that `_disposition`
    already recovers the same way.

    The FIRST figure, because a house advert reads "Prodej domu 180 m\u00b2,
    pozemek 600 m\u00b2" and the building is what this project compares. For
    a flat there is only one.
    """
    name = raw.get("advert_name")
    if not isinstance(name, str):
        return None
    match = _AREA_IN_NAME_RE.search(name)
    if not match:
        return None
    return safe_float(match.group(1).replace(",", "."))


def _area_from_unit_price(raw: dict) -> Optional[float]:
    """Area implied by price / price-per-square-metre.

    A second, independent route, used when the title does not state one. Less
    exact - the unit price is rounded, so this lands within a few tenths -
    which is why it is the fallback and not the primary.
    """
    total = safe_float(raw.get("price_czk")) or safe_float(raw.get("price"))
    per_m2 = safe_float(raw.get("price_czk_m2"))
    if not total or not per_m2:
        return None
    area = total / per_m2
    # Outside this range the two numbers are not what they were taken for.
    if not 5.0 <= area <= 2000.0:
        return None
    return round(area, 1)


def _area(raw: dict) -> Optional[float]:
    """Floor area, from whichever of the three sources has it.

    `usable_area` first because detail rows do carry it and it is the
    register's own figure; then the title; then the unit price.
    """
    for source in (safe_float(raw.get("usable_area")),
                   _area_from_name(raw),
                   _area_from_unit_price(raw)):
        if source:
            return source
    return None


def parse_estate(raw: dict) -> dict:
    """Pull every field this project wants out of one estate object.

    Deliberately shared between the index and detail endpoints: both are the
    same API serving the same object, and a live probe showed index rows
    already carry `locality`, `category_sub_cb` and `advert_name` - not just
    an id and a price, as this module originally assumed.

    That assumption was expensive: it meant one detail request per listing,
    i.e. ~12,000 requests to cover Prague. Parsing the index row first and
    only falling back to the detail endpoint for what is genuinely missing
    (see `is_complete`) turns the common case into ~30 requests for the same
    coverage - which matters most precisely because sreality's robots.txt
    does not invite this traffic in the first place.
    """
    loc = raw.get("locality") or {}
    if not isinstance(loc, dict):
        loc = {}

    address_parts = [
        _seo(loc.get("street_seo_name")),
        _seo(loc.get("citypart_seo_name")),
        _seo(loc.get("city_seo_name")),
    ]
    address = ", ".join(p.replace("-", " ").title() for p in address_parts if p) or None

    description = raw.get("advert_description")
    description = description.strip() if isinstance(description, str) else None

    property_type = MAIN_SLUG.get(_cb_value(raw.get("category_main_cb")))
    transaction_type = {1: "prodej", 2: "pronajem"}.get(_cb_value(raw.get("category_type_cb")))

    url = _build_source_url(raw)
    if url is None and property_type and transaction_type:
        url = _fallback_url(property_type, transaction_type, address, str(raw.get("hash_id") or ""))

    return {
        "price": _extract_index_price(raw),
        "area_m2": _area(raw),
        "floor": safe_int(raw.get("floor_number")),
        "disposition": _disposition(raw),
        "description": description,
        "lat": safe_float(loc.get("gps_lat")),
        "lon": safe_float(loc.get("gps_lon")),
        "address": address,
        "url": url,
        "property_type": property_type,
        "transaction_type": transaction_type,
    }


def is_complete(parsed: dict) -> bool:
    """True if an index row already told us everything a detail fetch would.

    GPS is the one that genuinely cannot be guessed - it is the geographic
    gate for a new listing - so a row without it always costs a detail
    request. Area and disposition matter for deduplication.
    """
    return all(parsed.get(key) is not None for key in ("lat", "lon", "area_m2", "disposition"))


def fetch_all(
    session: requests.Session,
    budget: Optional[Budget] = None,
    transactions: Optional[Iterable[str]] = None,
    districts: Optional[Iterable[int]] = None,
) -> tuple[list[NormalizedListing], list[dict], list[str], set[tuple[str, str]]]:
    """Walk the /search index for every (byt/dum) x (prodej/pronajem) x
    DISTRICT_IDS combo, returning one NormalizedListing per result.

    Index rows are rich - see the module docstring. `parse_estate` reads GPS,
    area, disposition, price and the locality straight out of them, and
    `run.py` falls back to `fetch_detail` only for the rows where something
    it needs is genuinely absent (`is_complete` decides).

    This paragraph used to say the opposite - that GPS and area were
    detail-only and deliberately not fetched here - which was true of the
    first version and was left behind when the index turned out to carry
    them. It cost real time: it is the reason a city-wide coordinate donor
    walk was first believed to need 12,000 detail requests rather than the
    ~25 index pages it actually takes.

    Never raises: a failure on one (property_type, transaction_type,
    district) slice is recorded in `errors` and that slice is abandoned,
    but every other slice still gets a chance.

    Returns (normalized_listings, raw_pages, errors, completed_scopes),
    where `completed_scopes` holds the (property_type, transaction_type)
    pairs whose every district walked cleanly to its natural end. Only
    those scopes may be used for absence-marking: with three districts and
    four categories this is twelve independent slices, and treating "any
    error anywhere" as "the whole source is unreliable" would mean one
    permanently-bad district id (56/57 are explicitly unverified - see
    module docstring) silently disables removal detection forever.
    """
    normalized: list[NormalizedListing] = []
    raw_pages: list[dict] = []
    errors: list[str] = []
    completed_scopes: set[tuple[str, str]] = set()

    wanted = set(transactions) if transactions else set(CATEGORY_TYPE)
    for property_type, main_cb in CATEGORY_MAIN.items():
        for transaction_type, type_cb in CATEGORY_TYPE.items():
            if transaction_type not in wanted:
                continue
            scope_ok = True
            for district_id in (tuple(districts) if districts else DISTRICT_IDS):
                offset = 0
                walked_to_end = False
                for _ in range(MAX_PAGES_SAFETY):
                    if budget is not None and budget.time_exhausted():
                        errors.append(
                            interruptions.interruption(
                                f"sreality {property_type}/{transaction_type}/district={district_id}: "
                                f"stopped at offset {offset} - run time budget exhausted"
                            )
                        )
                        scope_ok = False
                        break

                    params = {
                        "category_main_cb": main_cb,
                        "category_type_cb": type_cb,
                        "locality_country_id": COUNTRY_ID,
                        "locality_district_id": district_id,
                        "limit": PER_PAGE,
                        "offset": offset,
                    }
                    try:
                        payload = net.fetch_json(session, INDEX_URL, params=params)
                    except net.RequestFailed as exc:
                        errors.append(
                            f"sreality {property_type}/{transaction_type}/district={district_id} "
                            f"offset={offset}: {exc}"
                        )
                        scope_ok = False
                        break  # abandon this slice, move to the next one

                    raw_pages.append(
                        {
                            "kind": "index",
                            "property_type": property_type,
                            "transaction_type": transaction_type,
                            "district_id": district_id,
                            "offset": offset,
                            "response": payload,
                        }
                    )

                    results = payload.get("results") or []
                    for raw_item in results:
                        try:
                            source_id = _extract_index_id(raw_item)
                            if source_id is None:
                                continue
                            parsed = parse_estate(raw_item)
                            listing = NormalizedListing(
                                source=SOURCE_NAME,
                                source_id=source_id,
                                # Empty when the index row was too thin to
                                # build a real URL; the detail pass fills it.
                                url=parsed["url"] or "",
                                property_type=property_type,
                                transaction_type=transaction_type,
                                disposition=parsed["disposition"],
                                area_m2=parsed["area_m2"],
                                floor=parsed["floor"],
                                lat=parsed["lat"],
                                lon=parsed["lon"],
                                address=parsed["address"],
                                description=parsed["description"],
                                price=parsed["price"],
                            )
                            listing.extra["index_complete"] = is_complete(parsed)
                            if parsed["lat"] is not None and parsed["lon"] is not None:
                                # Tag geography here, while the coordinates
                                # are in hand. Doing it only in run.py meant
                                # an already-known listing came back with
                                # real GPS but a default priority_zone of
                                # False - which merge_source would then
                                # write over the correct stored value,
                                # quietly clearing the Sporilov/Hostivar
                                # flag on the second run.
                                listing.in_target_area = is_in_target_area(parsed["lat"], parsed["lon"])
                                listing.priority_zone = is_priority_zone(parsed["lat"], parsed["lon"])
                        except Exception:
                            # One malformed record must never abort the run.
                            continue
                        normalized.append(listing)

                    pagination = payload.get("pagination") or {}
                    total = pagination.get("total")
                    served_limit = pagination.get("limit")
                    net.polite_sleep()

                    if not results:
                        walked_to_end = True
                        break
                    offset += len(results)
                    if isinstance(total, int) and offset >= total:
                        walked_to_end = True
                        break
                    if len(results) < (served_limit or PER_PAGE):
                        walked_to_end = True
                        break

                if not walked_to_end:
                    # Either an error broke out above, or we ran into
                    # MAX_PAGES_SAFETY without reaching the end - in both
                    # cases this slice's coverage is unknown.
                    scope_ok = False

            # A NARROWED walk must never report a scope complete.
            #
            # completed_scopes is what absence marking runs off: a scope
            # reported complete means "every listing of this kind was walked,
            # so anything not seen is gone". Two districts out of the city is
            # not that, and claiming it marks every listing elsewhere as
            # vanished.
            #
            # This actually happened. The hourly pass was narrowed to Praha 4
            # and 10 while the retired weekly rent pass had already stored
            # rent listings from the whole city, and the next morning 1,201
            # live adverts in Vinohrady, Smichov, Zizkov and Karlin were
            # marked missing - not because they had gone, but because nothing
            # had looked for them and this said it had.
            #
            # scrapers/idnes.py has carried the same guard, under the same
            # name, since its own area walk was added. The reasoning that
            # sreality did not need one was that the watched belt sits inside
            # these two districts - true of the belt, and irrelevant to what
            # is actually stored.
            walked_whole_city = districts is None
            if scope_ok and walked_whole_city:
                completed_scopes.add((property_type, transaction_type))

    return normalized, raw_pages, errors, completed_scopes


def fetch_detail(session: requests.Session, source_id: str) -> tuple[Optional[dict], Optional[dict]]:
    """Fetch and parse the one-time detail record for a newly-seen listing.

    Returns (parsed, raw_response). `parsed` is a dict with keys
    {price, area_m2, floor, disposition, description, lat, lon, address, url}
    (all best-effort, individually possibly None) - or None if the fetch
    failed or the response didn't carry a recognizable estate (gone/removed,
    or an unexpected shape). `raw_response` is the raw JSON body for
    archiving (may be present even when `parsed` is None, if we got *a*
    response we just couldn't make sense of), or None if the request itself
    failed outright.
    """
    url = DETAIL_URL_TMPL.format(id=source_id)
    try:
        raw_payload = net.fetch_json(session, url)
    except net.RequestFailed:
        return None, None

    raw = _unwrap_estate(raw_payload)
    if "category_main_cb" not in raw:
        # A 200 whose body carries no estate marker - most often a listing
        # that was removed between the index walk and this detail fetch.
        return None, raw_payload

    parsed = parse_estate(raw)
    return parsed, raw_payload
