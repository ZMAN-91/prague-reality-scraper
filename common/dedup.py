"""Cross-source duplicate clustering and re-listing detection.

Two related but distinct problems live here:

1. **Clustering** (`cluster_listings`): the *same physical unit* is often
   advertised more than once at the same time - by a different broker, or
   simultaneously on sreality and bezrealitky. We never merge or drop such
   rows (the brief is explicit: analysis-time decision, not scrape-time).
   Instead every listing that matches at least one other gets a shared
   `cluster_id` and a `dedup_confidence` describing how sure we are.

2. **Re-listing** (`find_relist_candidate`): a listing disappears (goes
   `removed`), and later a *new* ad appears that looks like the same unit
   being re-advertised (same broker trying again, owner switching agency,
   etc). That's stored as a link (`relisted_from`) on the new row, not a
   merge - the new ad is still its own row with its own history.

Both use the same "does this pair of listings look like the same physical
unit" heuristic (`_match_confidence`), just with different candidate pools
and an added time constraint for re-listing.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Union

from common.geo import haversine_distance_m
from common.schema import STATUS_REMOVED, make_cluster_id, parse_iso

# --- Matching thresholds ---------------------------------------------------
#
# Both sites geocode to building/entrance level at best, so tens of meters
# of GPS noise between two independent listings of the *same* flat is
# normal; on the other hand two *different* flats in the same large housing
# block can sit within a few meters of each other. Area and disposition
# therefore always have to agree too - GPS proximity alone never fires the
# match. Numbers below are deliberately conservative (bias towards missing
# a real duplicate rather than falsely clustering two different units) since
# nothing is deleted or merged - a missed cluster only means "analyse these
# as if independent", which is the safe direction to be wrong in.

GPS_EXACT_M = 25.0
GPS_HIGH_M = 60.0
GPS_MEDIUM_M = 200.0

AREA_EXACT_REL = 0.02  # 2%
AREA_EXACT_ABS_M2 = 1.0
AREA_HIGH_REL = 0.06  # 6%
AREA_MEDIUM_REL = 0.15  # 15%

# Grid cell size for spatial bucketing, deliberately coarser than
# GPS_MEDIUM_M so any pair within the medium threshold always shares a cell
# or an immediately adjacent one.
GRID_DEG = 0.004  # ~ 300-400 m at Prague's latitude

# How long after a listing is marked "removed" a similar-looking new ad is
# still considered a plausible re-listing of it. 12 months is generous on
# purpose: the brief's own use case (multi-year market analysis) cares more
# about not missing a genuine re-listing than about a rare false positive
# months later - and a false link only adds a cross-reference, it never
# merges or discards data.
RELISTING_LOOKBACK_DAYS = 365

# Clustering is about *concurrent* duplicates - the same unit advertised by
# two brokers, or on both portals at once. Two ads for the same flat three
# years apart are not duplicates, they're a re-listing (handled separately,
# see find_relist_candidate). So a candidate pair must also have overlapping
# on-market windows, with this much slack at each end to absorb the usual
# case of one broker's ad going up a few days before another's comes down.
#
# This was missing from the first implementation even though the brief asked
# for it ("překrývající se cena/čas"); it is both a correctness fix and the
# thing that keeps clustering affordable, since it means long-dead rows stop
# being compared against everything forever.
CLUSTER_TIME_TOLERANCE_DAYS = 30

# Only rows seen this recently take part in *deriving* new clusters. Older
# rows keep whatever cluster they were already assigned (cluster ids are
# sticky - see cluster_listings), they just stop being re-compared against
# the whole corpus every hour. Slightly wider than the tolerance above so
# nothing is missed at the boundary.
# A street with more concurrent listings than this is a data problem (or a
# whole new development marketed as one address), and comparing every pair in
# it would cost more than the matches are worth.
MAX_STREET_BUCKET = 400

# The largest number of listings this project will call one flat. Genuine
# duplicates are one ad repeated by a few agencies across a few portals -
# two to six rows. Anything larger is a development where every unit looks
# like every other, not a duplicate.
MAX_CLUSTER_SIZE = 8

# How far two prices may differ and still be the same flat. Agencies do
# advertise one property at slightly different figures, so this is not zero -
# but eight units in a new development have eight different prices, and that
# is the only thing in the data that tells them apart. Geometry cannot: they
# are genuinely within twenty metres of each other, genuinely the same
# disposition, and genuinely within a few square metres in size.
PRICE_MATCH_REL = 0.02

CLUSTER_RECENT_WINDOW_DAYS = 45


def _area_close(a: Optional[float], b: Optional[float], rel: float) -> bool:
    if a is None or b is None:
        return False
    if a <= 0 or b <= 0:
        return False
    diff = abs(a - b)
    return diff <= max(AREA_EXACT_ABS_M2, rel * max(a, b))


def _on_market_window(row: dict) -> Optional[tuple[datetime, datetime]]:
    """(start, end) of a listing's time on the market, as aware datetimes.

    `last_seen_at` is day-granular while `first_seen_at` is a full
    timestamp, so for a listing first seen today the "end" can parse as
    *earlier* than the start (today 00:00 vs today 14:03) - hence the max().
    """
    start = parse_iso(row.get("first_seen_at"))
    if start is None:
        return None
    end = parse_iso(row.get("last_seen_at")) or start
    return start, max(start, end)


def _windows_overlap(a: dict, b: dict, tolerance_days: int = CLUSTER_TIME_TOLERANCE_DAYS) -> bool:
    """True if two listings were plausibly on the market at the same time.

    Unparseable/missing timestamps return True: an unknown window must not
    silently veto an otherwise strong GPS+area+disposition match.
    """
    window_a = _on_market_window(a)
    window_b = _on_market_window(b)
    if window_a is None or window_b is None:
        return True
    tol = timedelta(days=tolerance_days)
    return window_a[0] - tol <= window_b[1] and window_b[0] - tol <= window_a[1]



# --- matching a listing that has no coordinates ---------------------------
#
# Reality.iDNES.cz publishes no GPS at all - no schema.org, no data-lat, the
# map is loaded separately - so every listing from that source has empty
# lat/lon. The geometric matcher below returns None the moment either side
# is missing coordinates, which meant that until this existed, not one iDNES
# listing could ever be recognised as the same flat as a sreality or
# bezrealitky ad. For a project whose whole point is comparing the same
# market across portals, that is not a rounding error.
#
# What is left to match on is the address, and the three sources write it
# three different ways:
#
#   sreality      "Nurmiho, Hostivar, Praha"            (SEO slugs, no diacritics)
#   bezrealitky   "Nurmiho 12, Praha 4 - Sporilov, Praha"
#   idnes         "Nurmiho, Praha 15 - Hostivar"        (real diacritics)
#
# The street is the first comma-separated part in all three; the house number
# is only sometimes there. So the key is that part, folded and stripped of
# digits.
_HOUSE_NUMBER_RE = re.compile(r"[\d/]+\s*$")


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    folded = re.sub(r"[^a-z0-9 ]+", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def locality_tokens(address: Optional[str]) -> set:
    """District/quarter words from an address, after the street.

    Prague has the same street name in several districts - Nadrazni runs
    through both Smichov and Branik - so street plus area plus disposition
    alone would merge two unrelated flats whenever one of the sources
    published no coordinates to contradict it. Everything after the first
    comma names the place, and the three sources all put *something* usable
    there:

        sreality      "Nurmiho, Hostivar, Praha"     -> {hostivar, praha}
        bezrealitky   "..., Praha 4 - Sporilov, Praha" -> {praha, 4, sporilov}
        idnes         "Nurmiho, Praha 15 - Hostivar" -> {praha, 15, hostivar}

    The bare "praha" and the district number are dropped: they are shared by
    half the city and would let anything match anything.
    """
    if not address or "," not in address:
        return set()
    rest = _fold(address.split(",", 1)[1])
    tokens = {token for token in rest.split() if len(token) > 2}
    return tokens - {"praha", "cz", "ceska", "republika"}


def street_key(address: Optional[str]) -> str:
    """The street name from an address, comparable across all three sources.

    Returns "" when there is nothing usable, and "" never matches "" - an
    address-less listing must not be paired with every other address-less
    listing in Prague.
    """
    if not address:
        return ""
    first = address.split(",")[0]
    first = _HOUSE_NUMBER_RE.sub("", first).strip()
    folded = _fold(first)
    # A bare district ("praha 4") is not a street and would match half the city.
    if not folded or re.fullmatch(r"praha( \d+)?", folded):
        return ""
    return folded


def _prices_contradict(a: dict, b: dict) -> bool:
    """True when both rows have a price and the two are too far apart.

    Price is not in listings.csv - it changes, so it lives in observations -
    and is passed into cluster_listings by the caller, which has it to hand.
    """
    price_a, price_b = _to_float(a.get("price")), _to_float(b.get("price"))
    if not price_a or not price_b:
        return False
    return abs(price_a - price_b) > max(price_a, price_b) * PRICE_MATCH_REL


def _prices_agree(a: dict, b: dict) -> bool:
    """True only when both prices are known and close. Unknown is not agreement."""
    price_a, price_b = _to_float(a.get("price")), _to_float(b.get("price"))
    if not price_a or not price_b:
        return False
    return abs(price_a - price_b) <= max(price_a, price_b) * PRICE_MATCH_REL


def _match_confidence(a: dict, b: dict, require_time_overlap: bool = True,
                      require_price_agreement: bool = True) -> Optional[str]:
    """Return "exact" | "high" | "medium" | None for a pair of listing rows.

    Rows are plain dicts using the listings.csv column names (works for
    dicts read back from CSV as well as freshly-built ones).

    `require_time_overlap` is True for clustering (concurrent duplicates)
    and False for re-listing detection, where the whole point is that the
    old ad ended before the new one began.
    """
    if a["property_type"] != b["property_type"]:
        return None
    if a["transaction_type"] != b["transaction_type"]:
        return None
    if require_time_overlap and not _windows_overlap(a, b):
        return None
    disp_a, disp_b = (a.get("disposition") or "").strip(), (b.get("disposition") or "").strip()
    if not disp_a or not disp_b or disp_a != disp_b:
        return None
    # The price has to AGREE, on both sides, at every tier - not merely fail
    # to contradict. Two adverts for one flat quote the same figure; two flats
    # in one new development do not, and geometry cannot tell them apart
    # because they share a footprint and a floor plan.
    #
    # "Not contradicting" let a priceless advert match anything, and a
    # priceless advert in a new development matched every unit in the
    # building. Worse, it then bridged them: 2.25M - none - 3.89M put two
    # flats that directly contradict each other into one cluster, which is
    # precisely what the clique rule was added to prevent. Requiring
    # agreement removes the bridge rather than patching around it.
    if require_price_agreement and not _prices_agree(a, b):
        return None

    area_a = _to_float(a.get("area_m2"))
    area_b = _to_float(b.get("area_m2"))

    lat_a, lon_a = a.get("lat"), a.get("lon")
    lat_b, lon_b = b.get("lat"), b.get("lon")
    if lat_a in (None, "") or lon_a in (None, "") or lat_b in (None, "") or lon_b in (None, ""):
        # No coordinates on at least one side - see street_key above. The
        # street plus the same disposition and a matching area is real
        # evidence, but it is not the same evidence as two points forty
        # metres apart, so it can never be better than "medium": one big
        # block of flats can hold a dozen identical 2+kk units on one street.
        # Nothing is ever merged or deleted on the strength of it - a cluster
        # is a label, and the rows stay separate and inspectable.
        key_a, key_b = street_key(a.get("address")), street_key(b.get("address"))
        if not key_a or key_a != key_b:
            return None
        if not _area_close(area_a, area_b, AREA_MEDIUM_REL):
            return None
        # The same street name in two different districts is a real thing in
        # Prague, and with no coordinates to contradict it the street alone
        # would merge two unrelated flats. When both addresses say where they
        # are, they have to agree; when one does not, the street carries it.
        places_a, places_b = locality_tokens(a.get("address")), locality_tokens(b.get("address"))
        if places_a and places_b and not (places_a & places_b):
            return None
        return "medium"
    distance_m = haversine_distance_m(float(lat_a), float(lon_a), float(lat_b), float(lon_b))

    # Two coordinates close together are not enough on their own once the
    # radius grows: 200 m inside a housing estate covers several buildings,
    # and Zabehlice duly produced a cluster spanning Jasminova, Hvozdikova,
    # Jahodova, Pracska and Kapradova - five streets, one "duplicate". When
    # both sides name a street, they have to name the same one.
    if _streets_contradict(a, b):
        return None

    if (
        distance_m <= GPS_EXACT_M
        and _area_close(area_a, area_b, AREA_EXACT_REL)
    ):
        return "exact"
    if distance_m <= GPS_HIGH_M and _area_close(area_a, area_b, AREA_HIGH_REL):
        return "high"
    if distance_m <= GPS_MEDIUM_M and _area_close(area_a, area_b, AREA_MEDIUM_REL):
        # Agreement on price is now required of every tier, above, so this
        # weakest one - 200 m apart and 15% different in size - no longer
        # needs to ask for it separately.
        return "medium"
    return None


def _streets_contradict(a: dict, b: dict) -> bool:
    """True when both rows name a street and the streets differ."""
    key_a, key_b = street_key(a.get("address")), street_key(b.get("address"))
    return bool(key_a) and bool(key_b) and key_a != key_b


def _to_float(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_CONFIDENCE_RANK = {"exact": 3, "high": 2, "medium": 1}


class _DisjointSet:
    def __init__(self, items: Iterable[str]):
        self._parent = {item: item for item in items}

    def find(self, item: str) -> str:
        root = item
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[item] != root:
            self._parent[item], item = root, self._parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra


def _grid_key(lat: float, lon: float) -> tuple[int, int]:
    return (int(lat / GRID_DEG), int(lon / GRID_DEG))


def cluster_listings(
    listings: dict[str, dict],
    now: Optional[datetime] = None,
    prices: Optional[dict] = None,
) -> None:
    """Assign/refresh `cluster_id` and `dedup_confidence` on every row, in place.

    `listings` maps internal_id -> row dict (the same structure held in
    listings.csv). Existing non-empty cluster_id values are preserved and
    reused (clusters are sticky across runs); only genuinely new matches get
    a freshly generated id.

    `now` overrides the wall clock used for the "recently seen" window (see
    CLUSTER_RECENT_WINDOW_DAYS) - tests pin it so fixtures don't silently
    age out of the comparison set.
    """
    ids = list(listings.keys())
    if not ids:
        return

    # Price is the one field that separates eight flats in one development
    # from eight ads for one flat, and it is not in listings.csv. The caller
    # has it (run.py keeps the last observed price per listing), so it is
    # attached here rather than re-read from the observation log.
    if prices:
        for internal_id, row in listings.items():
            price = prices.get(internal_id)
            if price is not None:
                row = dict(row)
                row["price"] = price
                listings[internal_id] = row

    # Spatial bucket for candidate pair generation.
    #
    # Only recently-seen rows take part: a row last seen years ago can no
    # longer time-overlap with anything new (see _windows_overlap), and it
    # keeps whatever cluster it already had through the sticky pre-union
    # above. Without this the hourly pass would re-compare the entire
    # multi-year corpus against itself, which is quadratic in the densest
    # grid cells (a Prague housing estate puts thousands of rows within a
    # few hundred metres of each other).
    reference_now = now or datetime.now(timezone.utc)
    recent_cutoff = reference_now - timedelta(days=CLUSTER_RECENT_WINDOW_DAYS)
    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    # A second index, by street name, so that a listing with no coordinates
    # can still be compared with one that has them. Without it the iDNES rows
    # - which never have GPS - would sit in no bucket at all and could not be
    # paired with anything, however obviously they were the same flat.
    street_buckets: dict[str, list[str]] = defaultdict(list)
    for internal_id, row in listings.items():
        window = _on_market_window(row)
        if window is not None and window[1] < recent_cutoff:
            continue
        lat, lon = _to_float(row.get("lat")), _to_float(row.get("lon"))
        if lat is not None and lon is not None:
            buckets[_grid_key(lat, lon)].append(internal_id)
        key = street_key(row.get("address"))
        if key:
            street_buckets[key].append(internal_id)

    # Every pair that matches directly. Deliberately NOT a union-find any
    # more: transitive chaining is what produced a 46-row "duplicate" of
    # one-bedroom flats on Honzikova measuring 25, 29, 30, 40 and 41 m2. No
    # two of those match each other, but each matched its neighbour, and the
    # chain swept them all into one cluster. A cluster is a claim that these
    # rows are the same flat, and a chain cannot support that claim.
    direct: dict[str, dict] = defaultdict(dict)

    def consider_pair(id_a: str, id_b: str) -> None:
        if id_a == id_b:
            return
        confidence = _match_confidence(listings[id_a], listings[id_b])
        if confidence is None:
            return
        direct[id_a][id_b] = confidence
        direct[id_b][id_a] = confidence

    seen_pairs = set()
    for (gx, gy), members in buckets.items():
        neighbourhood_ids: list[str] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbourhood_ids.extend(buckets.get((gx + dx, gy + dy), []))
        for id_a in members:
            for id_b in neighbourhood_ids:
                if id_a >= id_b:
                    continue
                pair = (id_a, id_b)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                consider_pair(id_a, id_b)

    # The same pass over the street index. Bounded because a Prague street
    # holds tens of concurrent listings, not thousands - but bounded
    # explicitly, because "surely not" is how a quadratic blow-up gets into
    # an hourly job that nobody is watching.
    for key, members in street_buckets.items():
        if len(members) > MAX_STREET_BUCKET:
            continue
        for index, id_a in enumerate(members):
            for id_b in members[index + 1:]:
                pair = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                consider_pair(*pair)

    # Build clusters around a seed, where every member matched the seed
    # itself. Seeds are taken most-connected first so the densest genuine
    # duplicate goes first, and ties break on internal_id so the result does
    # not depend on dictionary order.
    assigned: dict[str, str] = {}
    best_pair_confidence: dict[str, str] = {}
    clusters: list[list[str]] = []

    for seed in sorted(direct, key=lambda i: (-len(direct[i]), i)):
        if seed in assigned:
            continue
        members = [seed]
        for other, confidence in sorted(
            direct[seed].items(), key=lambda kv: (-_CONFIDENCE_RANK[kv[1]], kv[0])
        ):
            if other in assigned or len(members) >= MAX_CLUSTER_SIZE:
                continue
            # Matching the seed is not enough. Every member has to match
            # every other member - the cluster is a clique, not a star.
            #
            # A star still chains, just one hop instead of many: a seed 111 m
            # from each of two listings that are 222 m from each other pulls
            # both in, and the cluster then claims two flats are the same
            # when nothing ever compared them. It is the same failure that
            # produced the 46-row Honzikova cluster, only smaller and harder
            # to see. At a cap of eight members the comparison is free.
            if any(other not in direct.get(m, {}) for m in members):
                continue
            members.append(other)
            assigned[other] = seed
            best = best_pair_confidence.get(other)
            if best is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[best]:
                best_pair_confidence[other] = confidence
            seed_best = best_pair_confidence.get(seed)
            if seed_best is None or _CONFIDENCE_RANK[confidence] > _CONFIDENCE_RANK[seed_best]:
                best_pair_confidence[seed] = confidence
        if len(members) > 1:
            assigned[seed] = seed
            clusters.append(members)

    # Materialize. A previously assigned cluster_id is reused when the same
    # rows are still together, so a cluster keeps its identity across runs -
    # but it is no longer inherited unconditionally: a cluster built by the
    # old chaining rule must be allowed to break up, or one bad merge would
    # outlive every fix to the matcher.
    clustered: set = set()
    # An inherited id may only be inherited once. Two separate cliques can
    # each contain a row carrying the same old cluster_id - a cluster that has
    # since split in two, which is exactly what the matcher is supposed to be
    # able to do - and taking the first non-empty id in each would hand both
    # halves the same id. Downstream nothing can tell them apart, so the two
    # halves read as one cluster and the clique rule appears to have failed.
    #
    # Seen live: clu_e29a held eight adverts at U zakrutu, prices from 2.25M
    # to 4.1M, pairs of which directly contradict each other. They were never
    # one clique; they were two, wearing one name.
    taken: set = set()
    for members in clusters:
        clustered.update(members)
        cluster_id = next(
            (listings[m]["cluster_id"] for m in sorted(members)
             if listings[m].get("cluster_id")
             and listings[m]["cluster_id"] not in taken),
            None,
        ) or make_cluster_id(members)
        while cluster_id in taken:
            # make_cluster_id is a hash of the members, so a collision here
            # means a genuinely different set; salt until it is unique.
            cluster_id = make_cluster_id(sorted(members) + [cluster_id])
        taken.add(cluster_id)
        for member in members:
            listings[member]["cluster_id"] = cluster_id
            listings[member]["dedup_confidence"] = best_pair_confidence.get(member, "medium")

    for internal_id in ids:
        if internal_id not in clustered:
            # Matched nothing this run: a listing whose only cluster-mate was
            # edited, removed, or was never really the same flat.
            listings[internal_id]["cluster_id"] = ""
            listings[internal_id]["dedup_confidence"] = ""


class RemovedIndex:
    """Grid index over `removed` rows, for re-listing lookups.

    run.py checks every genuinely-new listing against the pool of removed
    ones. That pool only grows (nothing is ever pruned), so a linear scan
    per new listing is O(new x all-history-removed) every single hour -
    fine in year one, not fine in year five. Bucketing by the same grid the
    clustering pass uses turns it into a handful of comparisons.
    """

    def __init__(self, rows: Iterable[dict]) -> None:
        self._buckets: dict[tuple[int, int], list[dict]] = defaultdict(list)
        self.size = 0
        for row in rows:
            if row.get("status") != STATUS_REMOVED:
                continue
            lat, lon = _to_float(row.get("lat")), _to_float(row.get("lon"))
            if lat is None or lon is None:
                continue
            self._buckets[_grid_key(lat, lon)].append(row)
            self.size += 1

    def candidates(self, lat: Optional[float], lon: Optional[float]) -> list[dict]:
        if lat is None or lon is None:
            return []
        gx, gy = _grid_key(lat, lon)
        out: list[dict] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                out.extend(self._buckets.get((gx + dx, gy + dy), []))
        return out


def find_relist_candidate(
    new_row: dict,
    removed_rows: Union[Iterable[dict], RemovedIndex],
    new_first_seen_iso: str,
) -> Optional[str]:
    """Return the internal_id of the best `removed` listing this new ad
    plausibly re-lists, or None.

    Re-listing commonly happens through a different broker - or even the
    other portal - so callers should pass candidates from every source, not
    just `new_row`'s own. This function checks only the physical-unit
    signature plus the time ordering: the candidate must have been removed
    *before* the new ad appeared, and within RELISTING_LOOKBACK_DAYS of it.

    `removed_rows` may be a plain iterable of rows (convenient in tests) or
    a RemovedIndex, which narrows the scan to the new listing's own
    neighbourhood.
    """
    new_seen_at = parse_iso(new_first_seen_iso)
    if new_seen_at is None:
        return None

    if isinstance(removed_rows, RemovedIndex):
        candidates: Iterable[dict] = removed_rows.candidates(
            _to_float(new_row.get("lat")), _to_float(new_row.get("lon"))
        )
    else:
        candidates = removed_rows

    best_id = None
    best_rank = -1
    for row in candidates:
        if row.get("status") != STATUS_REMOVED:
            continue
        last_seen_at = parse_iso(row.get("last_seen_at"))
        if last_seen_at is None:
            continue
        if last_seen_at > new_seen_at:
            continue  # candidate must have been removed *before* the new ad appeared
        if new_seen_at - last_seen_at > timedelta(days=RELISTING_LOOKBACK_DAYS):
            continue

        # Time overlap is deliberately NOT required here: a re-listing is by
        # definition the *absence* of overlap (old ad gone, new ad up).
        # Price agreement is required of CLUSTERING - two adverts running at
        # the same time for one flat quote the same figure. It must not be
        # required here: a re-listing is the sequential case, and re-listing
        # at a different price is the normal one. Usually a lower one, which
        # is exactly the behaviour this project exists to watch.
        confidence = _match_confidence(new_row, row, require_time_overlap=False,
                                       require_price_agreement=False)
        if confidence is None:
            continue
        rank = _CONFIDENCE_RANK[confidence]
        if rank > best_rank:
            best_rank = rank
            best_id = row.get("internal_id")

    return best_id
